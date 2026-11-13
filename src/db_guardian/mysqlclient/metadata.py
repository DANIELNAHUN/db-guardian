"""Read server version and per-table metadata (exact row counts, CREATE TABLE)."""

from collections.abc import Callable
from dataclasses import dataclass

import pymysql

from db_guardian.mysqlclient.connection import ConnectionInfo

CONNECT_TIMEOUT_SECONDS = 10
# Bounds a silent network drop; generous because COUNT(*) on a large table is slow.
READ_TIMEOUT_SECONDS = 3600


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
        read_timeout=READ_TIMEOUT_SECONDS,
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


# --- Clone targets -----------------------------------------------------------

ER_BAD_DB_ERROR = 1049
# Without global privileges MySQL hides whether a database exists and answers 1044.
ER_DBACCESS_DENIED_ERROR = 1044


class UnknownDatabaseError(ValueError):
    def __init__(self, database: str) -> None:
        super().__init__(f"target database '{database}' does not exist or the user cannot access it")
        self.database = database


class TlsUnavailableError(ConnectionError):
    pass


@dataclass
class TargetSchema:
    server_version: str
    tables: dict[str, str]


TargetReader = Callable[[ConnectionInfo], TargetSchema]


def _connect_tls(conn: ConnectionInfo) -> pymysql.Connection:
    """Connect with TLS required. Self-signed server certificates are accepted.

    PyMySQL silently stays in plaintext when the server offers no TLS, so the
    session cipher is checked and the connection refused without one.
    """
    try:
        db = pymysql.connect(
            host=conn.host,
            port=conn.port,
            user=conn.user,
            password=conn.password,
            database=conn.database,
            charset="utf8mb4",
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            read_timeout=READ_TIMEOUT_SECONDS,
            ssl={"check_hostname": False, "verify_mode": False},
        )
    except pymysql.err.OperationalError as exc:
        if exc.args and exc.args[0] in (ER_BAD_DB_ERROR, ER_DBACCESS_DENIED_ERROR):
            raise UnknownDatabaseError(conn.database) from None
        raise
    if not _session_cipher(db):
        db.close()
        raise TlsUnavailableError(f"{conn.host}:{conn.port} does not offer TLS; refusing an unencrypted connection")
    return db


def _session_cipher(db: pymysql.Connection) -> str:
    with db.cursor() as cur:
        cur.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        row = cur.fetchone()
        return row[1] if row else ""


def target_session_cipher(conn: ConnectionInfo) -> str:
    with _connect_tls(conn) as db:
        return _session_cipher(db)


def target_schema(conn: ConnectionInfo) -> TargetSchema:
    """Server version and CREATE TABLE text of every base table in the target database."""
    with _connect_tls(conn) as db, db.cursor() as cur:
        cur.execute("SELECT VERSION()")
        version = cur.fetchone()[0]
        cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
        names = [row[0] for row in cur.fetchall()]
        tables = {}
        for name in names:
            cur.execute(f"SHOW CREATE TABLE {quote_identifier(name)}")
            tables[name] = cur.fetchone()[1]
        return TargetSchema(server_version=version, tables=tables)
