from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import status

from db_guardian.backups.models import Backup, BackupStatus, FileState
from db_guardian.clones.diff import diff_schema, version_warnings
from db_guardian.clones.guard import matches_source
from db_guardian.clones.models import Clone, CloneStatus
from db_guardian.clones.schemas import ClonePreviewOut, DifferingTableOut, MissingTableOut
from db_guardian.clones.redact import redact
from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.mysqlclient.metadata import UnknownDatabaseError

if TYPE_CHECKING:
    from db_guardian.container import Container


class CloneRejected(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def build_preview(container: "Container", backup_id: int, target: ConnectionInfo) -> ClonePreviewOut:
    """Read-only comparison of a backup with a clone target. Raises CloneRejected."""
    with container.session_factory() as session:
        backup = session.get(Backup, backup_id)
        if backup is None:
            raise CloneRejected(status.HTTP_404_NOT_FOUND, f"backup {backup_id} not found")
        if (
            backup.status != BackupStatus.SUCCESS
            or backup.file_state != FileState.PRESENT
            or not backup.file_path
            or not Path(backup.file_path).is_file()
        ):
            raise CloneRejected(status.HTTP_410_GONE, "backup file is no longer available")
        production = matches_source(session, target)
        if production is not None:
            raise CloneRejected(
                status.HTTP_409_CONFLICT,
                f"target matches backup source '{production.name}'; refusing to clone into production",
            )
        backup_tables = {t.name: t.create_sql for t in backup.tables}
        source_version = backup.server_version

    try:
        schema = container.target_reader(target)
    except UnknownDatabaseError as exc:
        raise CloneRejected(422, str(exc)) from None
    except Exception as exc:  # network, auth, TLS: report to the caller without the password
        message = redact(str(exc) or exc.__class__.__name__, target.password)
        raise CloneRejected(status.HTTP_502_BAD_GATEWAY, f"cannot read target: {message}") from None

    diff = diff_schema(backup_tables, schema.tables)
    return ClonePreviewOut(
        backup_id=backup_id,
        source_version=source_version,
        target_version=schema.server_version,
        missing_in_target=[MissingTableOut(name=n, create_sql=backup_tables[n]) for n in diff.missing_in_target],
        structure_differs=[
            DifferingTableOut(name=n, backup_create_sql=backup_tables[n], target_create_sql=schema.tables[n])
            for n in diff.structure_differs
        ],
        only_in_target=diff.only_in_target,
        matching=diff.matching,
        warnings=_warnings(source_version, schema, list(backup_tables.values())),
    )


def enqueue_clone(container: "Container", backup_id: int, target: ConnectionInfo) -> Clone:
    """Validate like the preview (raising CloneRejected synchronously), then queue the clone.

    The target password only lives in the queued job's closure.
    """
    build_preview(container, backup_id, target)
    with container.session_factory() as session:
        backup = session.get(Backup, backup_id)
        clone = Clone(
            backup_id=backup_id,
            source_name=backup.source_name,
            target_host=target.host,
            target_port=target.port,
            target_database=target.database,
            target_user=target.user,
            status=CloneStatus.QUEUED,
        )
        session.add(clone)
        session.commit()
    container.queue.submit(f"clone:{clone.id}", lambda: container.clone_runner.run(clone.id, target))
    return clone


BINLOG_WARNING = (
    "binary logging is on and log_bin_trust_function_creators is off on the target: "
    "creating triggers or routines will fail with error 1419 unless the clone user has SUPER; "
    "set log_bin_trust_function_creators = 1 on the target"
)


def _warnings(source_version: str | None, schema, backup_sql: list[str]) -> list[str]:
    warnings = version_warnings(source_version, schema.server_version, backup_sql)
    if schema.binlog_blocks_routines:
        warnings.append(BINLOG_WARNING)
    return warnings
