import logging
import time
from dataclasses import asdict
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from db_guardian.backups.models import Backup
from db_guardian.clones.diff import diff_schema, version_warnings
from db_guardian.clones.models import Clone, CloneStatus
from db_guardian.clones.redact import redact
from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.mysqlclient.metadata import TargetReader
from db_guardian.mysqlclient.restore import Restorer
from db_guardian.timefmt import utcnow

logger = logging.getLogger(__name__)

PROGRESS_STEP = 0.05


class CloneRunner:
    """Restores one backup into a target and records the outcome."""

    def __init__(
        self, session_factory: sessionmaker[Session], target_reader: TargetReader, restorer: Restorer
    ) -> None:
        self._session_factory = session_factory
        self.target_reader = target_reader
        self.restorer = restorer

    def run(self, clone_id: int, target: ConnectionInfo) -> None:
        with self._session_factory() as session:
            clone = session.get(Clone, clone_id)
            if clone is None:
                logger.error("clone %s vanished before running", clone_id)
                return
            clone.status = CloneStatus.RUNNING
            clone.started_at = utcnow()
            session.commit()
            clock = time.monotonic()
            try:
                backup = session.get(Backup, clone.backup_id)
                backup_tables = {t.name: t.create_sql for t in backup.tables}
                schema = self.target_reader(target)
                diff = diff_schema(backup_tables, schema.tables)
                clone.preview = {
                    "counts": {key: len(names) for key, names in asdict(diff).items()},
                    "warnings": version_warnings(
                        backup.server_version, schema.server_version, list(backup_tables.values())
                    ),
                }
                session.commit()

                self.restorer(target, Path(backup.file_path), on_progress=self._progress_saver(session, clone))

                clone.tables = [t.name for t in backup.tables]
                clone.progress = 1.0
                clone.status = CloneStatus.SUCCESS
                logger.info("clone %s of backup %s into %s succeeded", clone.id, backup.id, target.host)
            except Exception as exc:  # the worker must survive any failure
                session.rollback()
                clone = session.get(Clone, clone_id)
                clone.status = CloneStatus.FAILED
                clone.error = redact(str(exc) or exc.__class__.__name__, target.password)
                logger.error("clone %s failed: %s", clone_id, clone.error)
            clone.finished_at = utcnow()
            clone.duration_seconds = round(time.monotonic() - clock, 3)
            session.commit()

    @staticmethod
    def _progress_saver(session: Session, clone: Clone):
        def save(fraction: float) -> None:
            if fraction >= 1.0 or fraction - clone.progress >= PROGRESS_STEP:
                clone.progress = min(fraction, 1.0)
                session.commit()

        return save
