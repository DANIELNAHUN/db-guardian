import threading
from typing import TYPE_CHECKING

from sqlalchemy import select

from db_guardian.backups.models import Backup, BackupStatus, BackupTrigger
from db_guardian.sources.models import Source

if TYPE_CHECKING:
    from db_guardian.container import Container

_enqueue_lock = threading.Lock()


class SourceUnavailable(RuntimeError):
    """The source is disabled, deleted or unknown, so it cannot be backed up."""


def enqueue_backup(container: "Container", source_id: int, trigger: BackupTrigger) -> tuple[Backup, bool]:
    """Queue a backup of a source. Returns (backup, created).

    If a backup of this source is already queued, returns it with created=False.
    """
    with _enqueue_lock, container.session_factory() as session:
        source = session.get(Source, source_id)
        if source is None or source.deleted_at is not None or not source.enabled:
            raise SourceUnavailable(f"source {source_id} is disabled, deleted or unknown")

        existing = session.scalars(
            select(Backup)
            .where(Backup.source_id == source_id, Backup.status == BackupStatus.QUEUED)
            .order_by(Backup.id)
        ).first()
        if existing is not None:
            return existing, False

        backup = Backup(
            source_id=source_id, source_name=source.name, trigger=trigger, status=BackupStatus.QUEUED
        )
        session.add(backup)
        session.commit()
        backup_id = backup.id

    container.queue.submit(f"backup:{source_id}", lambda: container.runner.run(backup_id))
    return backup, True
