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
