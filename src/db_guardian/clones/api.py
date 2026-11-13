from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from db_guardian.clones.models import Clone, CloneStatus
from db_guardian.clones.schemas import CloneOut, ClonePage, ClonePreviewOut, TargetIn
from db_guardian.clones.security import require_clone_key
from db_guardian.clones.service import CloneRejected, build_preview, enqueue_clone
from db_guardian.container import Container
from db_guardian.deps import get_container
from db_guardian.timefmt import to_display

router = APIRouter(tags=["clones"])


def _http(exc: CloneRejected) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


@router.post(
    "/backups/{backup_id}/clone-preview",
    response_model=ClonePreviewOut,
    dependencies=[Depends(require_clone_key)],
)
def clone_preview(backup_id: int, target: TargetIn, c: Container = Depends(get_container)):
    """Compare a backup with the target database. Changes nothing."""
    try:
        return build_preview(c, backup_id, target.to_connection())
    except CloneRejected as exc:
        raise _http(exc) from None


def clone_out(clone: Clone, tz: str) -> CloneOut:
    return CloneOut(
        id=clone.id,
        backup_id=clone.backup_id,
        source_name=clone.source_name,
        target_host=clone.target_host,
        target_port=clone.target_port,
        target_database=clone.target_database,
        target_user=clone.target_user,
        status=clone.status,
        error=clone.error,
        created_at=to_display(clone.created_at, tz),
        started_at=to_display(clone.started_at, tz),
        finished_at=to_display(clone.finished_at, tz),
        duration_seconds=clone.duration_seconds,
        progress=clone.progress or 0.0,
        tables=clone.tables,
        preview=clone.preview,
    )


@router.post(
    "/backups/{backup_id}/clones",
    response_model=CloneOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_clone_key)],
)
def start_clone(backup_id: int, target: TargetIn, c: Container = Depends(get_container)):
    """Queue a clone of the backup into the target. Tables only in the target are left untouched."""
    try:
        clone = enqueue_clone(c, backup_id, target.to_connection())
    except CloneRejected as exc:
        raise _http(exc) from None
    return clone_out(clone, c.settings.display_tz)


@router.get("/clones", response_model=ClonePage)
def list_clones(
    backup_id: int | None = None,
    status_: CloneStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    c: Container = Depends(get_container),
):
    filters = []
    if backup_id is not None:
        filters.append(Clone.backup_id == backup_id)
    if status_ is not None:
        filters.append(Clone.status == status_)
    with c.session_factory() as session:
        total = session.scalar(select(func.count()).select_from(Clone).where(*filters))
        rows = session.scalars(
            select(Clone).where(*filters).order_by(Clone.id.desc()).limit(limit).offset(offset)
        ).all()
        return ClonePage(items=[clone_out(x, c.settings.display_tz) for x in rows], total=total or 0)


@router.get("/clones/{clone_id}", response_model=CloneOut)
def get_clone(clone_id: int, c: Container = Depends(get_container)):
    with c.session_factory() as session:
        clone = session.get(Clone, clone_id)
        if clone is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"clone {clone_id} not found")
        return clone_out(clone, c.settings.display_tz)
