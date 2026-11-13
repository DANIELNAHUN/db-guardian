import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from db_guardian.backups.models import Backup, BackupStatus
from db_guardian.clones.models import Clone, CloneStatus
from db_guardian.mysqlclient.dump import DUMP_WORKDIR_PREFIX
from db_guardian.timefmt import utcnow

INTERRUPTED = "interrupted: service stopped before completion"


def recover_interrupted(session_factory: sessionmaker[Session], backups_dir: Path) -> int:
    """Fail backups and clones left queued/running by a previous process; remove partial files.

    Clones are not retried: their target password only lived in memory.
    """
    with session_factory() as session:
        stale = list(session.scalars(
            select(Backup).where(Backup.status.in_([BackupStatus.QUEUED, BackupStatus.RUNNING]))
        ))
        now = utcnow()
        for backup in stale:
            backup.status = BackupStatus.FAILED
            backup.error = INTERRUPTED
            backup.finished_at = now
        stale_clones = list(session.scalars(
            select(Clone).where(Clone.status.in_([CloneStatus.QUEUED, CloneStatus.RUNNING]))
        ))
        for clone in stale_clones:
            clone.status = CloneStatus.FAILED
            clone.error = INTERRUPTED
            clone.finished_at = now
        session.commit()

    if backups_dir.exists():
        for partial in backups_dir.rglob("*.tmp"):
            partial.unlink(missing_ok=True)
    return len(stale) + len(stale_clones)


def sweep_dump_workdirs(temp_dir: Path) -> int:
    """Remove dump work dirs (they hold a plaintext option file) left by a killed process."""
    removed = 0
    for leftover in temp_dir.glob(f"{DUMP_WORKDIR_PREFIX}*"):
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
            removed += 1
    return removed
