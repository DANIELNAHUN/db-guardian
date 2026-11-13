from fastapi import APIRouter, Depends, HTTPException

from db_guardian.clones.schemas import ClonePreviewOut, TargetIn
from db_guardian.clones.security import require_clone_key
from db_guardian.clones.service import CloneRejected, build_preview
from db_guardian.container import Container
from db_guardian.deps import get_container

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
