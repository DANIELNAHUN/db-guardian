from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from db_guardian.backups.models import Backup, BackupStatus
from db_guardian.backups.schemas import BackupDetailOut, BackupPage, backup_detail_out, backup_out
from db_guardian.container import Container
from db_guardian.deps import get_container
from db_guardian.timefmt import from_client

router = APIRouter(prefix="/backups", tags=["backups"])


@router.get("", response_model=BackupPage)
def list_backups(
    source_id: int | None = None,
    status_: BackupStatus | None = Query(default=None, alias="status"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    c: Container = Depends(get_container),
):
    tz = c.settings.display_tz
    filters = []
    if source_id is not None:
        filters.append(Backup.source_id == source_id)
    if status_ is not None:
        filters.append(Backup.status == status_)
    if date_from is not None:
        filters.append(Backup.created_at >= from_client(date_from, tz))
    if date_to is not None:
        filters.append(Backup.created_at <= from_client(date_to, tz))

    with c.session_factory() as session:
        total = session.scalar(select(func.count()).select_from(Backup).where(*filters))
        rows = session.scalars(
            select(Backup).where(*filters).order_by(Backup.id.desc()).limit(limit).offset(offset)
        ).all()
        return BackupPage(items=[backup_out(b, tz) for b in rows], total=total or 0)


@router.get("/{backup_id}", response_model=BackupDetailOut)
def get_backup(backup_id: int, c: Container = Depends(get_container)):
    with c.session_factory() as session:
        backup = session.get(Backup, backup_id)
        if backup is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"backup {backup_id} not found")
        return backup_detail_out(backup, c.settings.display_tz)
