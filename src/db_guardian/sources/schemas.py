from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field, field_validator

from db_guardian.sources.models import ALL_TABLES


def validate_tables(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    cleaned = [name.strip() for name in value]
    if not cleaned:
        raise ValueError('must not be empty; use ["*"] for all tables')
    if any(not name for name in cleaned):
        raise ValueError("table names must not be blank")
    if ALL_TABLES in cleaned and len(cleaned) > 1:
        raise ValueError('"*" cannot be combined with table names')
    return cleaned


def validate_schedule(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        CronTrigger.from_crontab(value)
    except ValueError as exc:
        raise ValueError(f"invalid cron expression: {exc}") from exc
    return value


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1)
    port: int = Field(default=3306, ge=1, le=65535)
    user: str = Field(min_length=1)
    password: str
    database: str = Field(min_length=1)
    tables: list[str]
    schedule: str
    retention: int | None = Field(default=None, ge=1)
    enabled: bool = True

    _tables = field_validator("tables")(validate_tables)
    _schedule = field_validator("schedule")(validate_schedule)


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    host: str | None = Field(default=None, min_length=1)
    port: int | None = Field(default=None, ge=1, le=65535)
    user: str | None = Field(default=None, min_length=1)
    password: str | None = None
    database: str | None = Field(default=None, min_length=1)
    tables: list[str] | None = None
    schedule: str | None = None
    retention: int | None = Field(default=None, ge=1)
    enabled: bool | None = None

    _tables = field_validator("tables")(validate_tables)
    _schedule = field_validator("schedule")(validate_schedule)


class SourceOut(BaseModel):
    id: int
    name: str
    host: str
    port: int
    user: str
    database: str
    tables: list[str]
    schedule: str
    retention: int
    enabled: bool
    has_password: bool
    created_at: str | None
    updated_at: str | None
