from pydantic import BaseModel

from db_guardian.backups.models import Backup
from db_guardian.timefmt import to_display


class BackupOut(BaseModel):
    id: int
    source_id: int
    source_name: str
    trigger: str
    status: str
    error: str | None
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None
    file_size: int | None
    server_version: str | None
    file_state: str


class BackupTableOut(BaseModel):
    name: str
    row_count: int


class BackupDetailOut(BackupOut):
    table_count: int
    total_rows: int
    tables: list[BackupTableOut]


class BackupPage(BaseModel):
    items: list[BackupOut]
    total: int


def backup_out(backup: Backup, tz: str) -> BackupOut:
    return BackupOut(**_common(backup, tz))


def backup_detail_out(backup: Backup, tz: str) -> BackupDetailOut:
    tables = [BackupTableOut(name=t.name, row_count=t.row_count) for t in backup.tables]
    return BackupDetailOut(
        **_common(backup, tz),
        table_count=len(tables),
        total_rows=sum(t.row_count for t in tables),
        tables=tables,
    )


def _common(backup: Backup, tz: str) -> dict:
    return {
        "id": backup.id,
        "source_id": backup.source_id,
        "source_name": backup.source_name,
        "trigger": backup.trigger,
        "status": backup.status,
        "error": backup.error,
        "created_at": to_display(backup.created_at, tz),
        "started_at": to_display(backup.started_at, tz),
        "finished_at": to_display(backup.finished_at, tz),
        "duration_seconds": backup.duration_seconds,
        "file_size": backup.file_size,
        "server_version": backup.server_version,
        "file_state": backup.file_state,
    }
