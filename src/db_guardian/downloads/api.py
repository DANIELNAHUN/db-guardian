import re
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from db_guardian.backups.models import Backup, BackupStatus, FileState
from db_guardian.container import Container
from db_guardian.deps import get_container
from db_guardian.downloads.signing import sign, verify
from db_guardian.timefmt import to_display

router = APIRouter(tags=["downloads"])


class DownloadUrlOut(BaseModel):
    url: str
    expires_at: str


def _now() -> int:
    return int(time.time())


def _gone() -> HTTPException:
    return HTTPException(status.HTTP_410_GONE, "backup file is no longer available")


def _download_name(backup: Backup, tz: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", backup.source_name)
    when = (backup.started_at or backup.created_at).replace(tzinfo=UTC).astimezone(ZoneInfo(tz))
    return f"{safe_name}_{when:%Y%m%d_%H%M%S}.sql.gz"


def _available_file(backup: Backup) -> Path:
    if backup.status != BackupStatus.SUCCESS or backup.file_state != FileState.PRESENT:
        raise _gone()
    path = Path(backup.file_path or "")
    if not backup.file_path or not path.is_file():
        raise _gone()
    return path


@router.post("/backups/{backup_id}/download-url", response_model=DownloadUrlOut)
def create_download_url(backup_id: int, c: Container = Depends(get_container)):
    with c.session_factory() as session:
        backup = session.get(Backup, backup_id)
        if backup is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"backup {backup_id} not found")
        _available_file(backup)
    expires_at = _now() + c.settings.download_ttl_seconds
    sig = sign(backup_id, expires_at, c.settings.signing_key)
    expires_display = to_display(datetime.fromtimestamp(expires_at, UTC).replace(tzinfo=None), c.settings.display_tz)
    return DownloadUrlOut(url=f"/downloads/{backup_id}?exp={expires_at}&sig={sig}", expires_at=expires_display)


@router.get("/downloads/{backup_id}")
def download(backup_id: int, exp: int, sig: str, c: Container = Depends(get_container)):
    # The signature is checked before touching the database.
    if not verify(backup_id, exp, sig, c.settings.signing_key, now=_now()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid or expired download link")
    with c.session_factory() as session:
        backup = session.get(Backup, backup_id)
        if backup is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"backup {backup_id} not found")
        path = _available_file(backup)
        filename = _download_name(backup, c.settings.display_tz)
    return FileResponse(path, media_type="application/gzip", filename=filename)
