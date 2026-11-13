from pydantic import BaseModel, Field

from db_guardian.mysqlclient.connection import ConnectionInfo


class TargetIn(BaseModel):
    """Connection data of the clone target. The password is never stored."""

    host: str = Field(min_length=1)
    port: int = Field(default=3306, ge=1, le=65535)
    user: str = Field(min_length=1)
    password: str
    database: str = Field(min_length=1)

    def to_connection(self) -> ConnectionInfo:
        return ConnectionInfo(
            host=self.host, port=self.port, user=self.user, password=self.password, database=self.database
        )


class MissingTableOut(BaseModel):
    name: str
    create_sql: str


class DifferingTableOut(BaseModel):
    name: str
    backup_create_sql: str
    target_create_sql: str


class ClonePreviewOut(BaseModel):
    backup_id: int
    source_version: str | None
    target_version: str
    missing_in_target: list[MissingTableOut]
    structure_differs: list[DifferingTableOut]
    only_in_target: list[str]
    matching: list[str]
    warnings: list[str]


class CloneOut(BaseModel):
    id: int
    backup_id: int
    source_name: str
    target_host: str
    target_port: int
    target_database: str
    target_user: str
    status: str
    error: str | None
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None
    progress: float
    tables: list[str] | None
    preview: dict | None


class ClonePage(BaseModel):
    items: list[CloneOut]
    total: int
