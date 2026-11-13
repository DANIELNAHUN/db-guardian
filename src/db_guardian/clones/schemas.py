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
