from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from db_guardian.backups.models import Backup, BackupStatus
from db_guardian.timefmt import utcnow

INTERRUPTED = "interrupted: service stopped before completion"


def recover_interrupted(session_factory: sessionmaker[Session], backups_dir: Path) -> int:
    """Fail backups left queued/running by a previous process and remove partial files."""
    with session_factory() as session:
        stale = list(session.scalars(
            select(Backup).where(Backup.status.in_([BackupStatus.QUEUED, BackupStatus.RUNNING]))
        ))
        now = utcnow()
        for backup in stale:
            backup.status = BackupStatus.FAILED
            backup.error = INTERRUPTED
            backup.finished_at = now
        session.commit()

    if backups_dir.exists():
        for partial in backups_dir.rglob("*.tmp"):
            partial.unlink(missing_ok=True)
    return len(stale)
