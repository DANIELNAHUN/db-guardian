import logging
import os
import time
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from db_guardian.backups.models import Backup, BackupStatus, BackupTable, FileState
from db_guardian.backups.retention import apply_retention
from db_guardian.crypto import SecretBox
from db_guardian.mysqlclient.dump import Dumper
from db_guardian.mysqlclient.metadata import MetadataReader
from db_guardian.sources.models import Source, selected_tables
from db_guardian.sources.service import SourceService
from db_guardian.timefmt import utcnow

logger = logging.getLogger(__name__)


class BackupRunner:
    """Runs one queued backup end to end and records the outcome."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        box: SecretBox,
        backups_dir: Path,
        dumper: Dumper,
        metadata_reader: MetadataReader,
    ) -> None:
        self._session_factory = session_factory
        self._box = box
        self._backups_dir = backups_dir
        self._dumper = dumper
        self._metadata_reader = metadata_reader

    def run(self, backup_id: int) -> None:
        with self._session_factory() as session:
            backup = session.get(Backup, backup_id)
            if backup is None:
                logger.error("backup %s vanished before running", backup_id)
                return
            backup.status = BackupStatus.RUNNING
            backup.started_at = utcnow()
            session.commit()

            clock = time.monotonic()
            dest: Path | None = None
            try:
                source = session.get(Source, backup.source_id)
                if source is None or source.deleted_at is not None:
                    raise RuntimeError("source was deleted before the backup ran")
                conn = SourceService(session, self._box).connection(source)
                tables = selected_tables(source.tables)

                meta = self._metadata_reader(conn, tables)
                dest = self._destination(backup)
                tmp = dest.with_suffix(".tmp")
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    size = self._dumper(conn, tables, meta.server_version, tmp)
                    os.replace(tmp, dest)
                finally:
                    tmp.unlink(missing_ok=True)

                backup.tables = [
                    BackupTable(name=t.name, row_count=t.row_count, create_sql=t.create_sql)
                    for t in meta.tables
                ]
                backup.server_version = meta.server_version
                backup.file_path = str(dest)
                backup.file_size = size
                backup.file_state = FileState.PRESENT
                backup.status = BackupStatus.SUCCESS
                self._finish(session, backup, clock)
                logger.info("backup %s of %s succeeded (%s bytes)", backup.id, backup.source_name, size)
            except Exception as exc:  # the worker must survive any failure
                session.rollback()
                if dest is not None:
                    dest.unlink(missing_ok=True)
                backup = session.get(Backup, backup_id)
                backup.status = BackupStatus.FAILED
                backup.error = str(exc) or exc.__class__.__name__
                backup.file_state = FileState.NONE
                self._finish(session, backup, clock)
                logger.exception("backup %s of %s failed", backup_id, backup.source_name)
                return

            # Retention runs only after the success is committed; its failure must
            # never turn a good backup into a failed one.
            try:
                apply_retention(session, source.id, source.retention)
            except Exception:
                session.rollback()
                logger.exception("retention failed for source %s", source.id)

    def _destination(self, backup: Backup) -> Path:
        stamp = backup.started_at.strftime("%Y%m%dT%H%M%SZ")
        return self._backups_dir / str(backup.source_id) / f"{stamp}_{backup.id}.sql.gz"

    @staticmethod
    def _finish(session: Session, backup: Backup, clock: float) -> None:
        backup.finished_at = utcnow()
        backup.duration_seconds = round(time.monotonic() - clock, 3)
        session.commit()
