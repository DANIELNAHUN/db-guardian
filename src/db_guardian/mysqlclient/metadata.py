"""Read server version and per-table metadata (exact row counts, CREATE TABLE)."""

from collections.abc import Callable
from dataclasses import dataclass

import pymysql

from db_guardian.mysqlclient.connection import ConnectionInfo

CONNECT_TIMEOUT_SECONDS = 10


class MissingTablesError(ValueError):
    def __init__(self, missing: list[str]) -> None:
        super().__init__("tables not found in source: " + ", ".join(missing))
        self.missing = missing


@dataclass
class TableMeta:
    name: str
    row_count: int
    create_sql: str


@dataclass
class SnapshotMeta:
    server_version: str
    tables: list[TableMeta]


MetadataReader = Callable[[ConnectionInfo, list[str] | None], SnapshotMeta]


def quote_identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _connect(conn: ConnectionInfo) -> pymysql.Connection:
    return pymysql.connect(
        host=conn.host,
        port=conn.port,
        user=conn.user,
        password=conn.password,
        database=conn.database,
        charset="utf8mb4",
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )


def server_version(conn: ConnectionInfo) -> str:
    with _connect(conn) as db, db.cursor() as cur:
        cur.execute("SELECT VERSION()")
        return cur.fetchone()[0]


def capture_metadata(conn: ConnectionInfo, tables: list[str] | None) -> SnapshotMeta:
    """Exact row counts and CREATE TABLE for the selected base tables (None = all).

    Requested names are validated against SHOW TABLES before any query uses them.
    """
    with _connect(conn) as db, db.cursor() as cur:
        cur.execute("SELECT VERSION()")
        version = cur.fetchone()[0]
        cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
        available = [row[0] for row in cur.fetchall()]

        if tables is None:
            names = available
        else:
            known = set(available)
            missing = [name for name in tables if name not in known]
            if missing:
                raise MissingTablesError(missing)
            names = list(tables)

        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        result = []
        for name in names:
            quoted = quote_identifier(name)
            cur.execute(f"SELECT COUNT(*) FROM {quoted}")
            row_count = int(cur.fetchone()[0])
            cur.execute(f"SHOW CREATE TABLE {quoted}")
            result.append(TableMeta(name=name, row_count=row_count, create_sql=cur.fetchone()[1]))
        cur.execute("COMMIT")
        return SnapshotMeta(server_version=version, tables=result)
