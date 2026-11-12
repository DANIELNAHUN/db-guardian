from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from db_guardian.backups.models import Backup, BackupStatus, FileState


def apply_retention(session: Session, source_id: int, keep: int) -> list[int]:
    """Keep the `keep` newest successful backup files of a source; delete older files.

    History rows are never deleted, and failed backups never count.
    Returns the ids of the backups whose file was deleted.
    """
    stmt = (
        select(Backup)
        .where(
            Backup.source_id == source_id,
            Backup.status == BackupStatus.SUCCESS,
            Backup.file_state == FileState.PRESENT,
        )
        .order_by(Backup.finished_at.desc(), Backup.id.desc())
    )
    expired = list(session.scalars(stmt))[keep:]
    for backup in expired:
        if backup.file_path:
            Path(backup.file_path).unlink(missing_ok=True)
        backup.file_state = FileState.DELETED_BY_RETENTION
    session.commit()
    return [backup.id for backup in expired]
