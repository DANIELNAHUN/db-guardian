import gzip
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pymysql
import pytest

from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.mysqlclient.dump import write_option_file

GUARDIAN_PASSWORD = 'p#ss "w\\rd"'
SERVERS = {"mysql57": "IT_MYSQL57_HOST", "mysql80": "IT_MYSQL80_HOST"}


def _host(env_var: str) -> str:
    host = os.environ.get(env_var)
    if not host:
        pytest.skip(f"{env_var} not set (run via docker-compose.test.yml)")
    return host


@pytest.fixture(params=sorted(SERVERS))
def server(request) -> str:
    return _host(SERVERS[request.param])


@pytest.fixture
def conn(server) -> ConnectionInfo:
    return ConnectionInfo(host=server, port=3306, user="guardian", password=GUARDIAN_PASSWORD, database="shop")


@pytest.fixture
def root(server) -> ConnectionInfo:
    return ConnectionInfo(
        host=server, port=3306, user="root", password=os.environ.get("IT_ROOT_PASSWORD", "root"), database="mysql"
    )


def root_query(root: ConnectionInfo, sql: str, database: str | None = None) -> list[tuple]:
    with pymysql.connect(
        host=root.host, port=root.port, user=root.user, password=root.password, database=database
    ) as db, db.cursor() as cur:
        cur.execute(sql)
        rows = list(cur.fetchall())
        db.commit()
        return rows


def restore(root: ConnectionInfo, dump_file: Path, database: str) -> None:
    """Restore a .sql.gz into a fresh database using the mysql client."""
    root_query(root, f"DROP DATABASE IF EXISTS `{database}`")
    root_query(root, f"CREATE DATABASE `{database}`")
    workdir = Path(tempfile.mkdtemp())
    try:
        option_file = write_option_file(root, workdir)
        subprocess.run(
            ["mysql", f"--defaults-extra-file={option_file}", database],
            input=gzip.decompress(dump_file.read_bytes()),
            check=True,
            capture_output=True,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture
def sql(root):
    """Run SQL as root: sql("SELECT ...", database=None) -> rows."""
    return lambda statement, database=None: root_query(root, statement, database)


@pytest.fixture
def restore_into(root):
    """Restore a .sql.gz into a fresh database: restore_into(path, database)."""
    return lambda dump_file, database: restore(root, dump_file, database)


STAGER_PASSWORD = 'st@ge"r#1'
STAGER_PRIVILEGES = (
    "SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES, CREATE VIEW, "
    "SHOW VIEW, TRIGGER, CREATE ROUTINE, ALTER ROUTINE, EVENT, LOCK TABLES"
)


@pytest.fixture
def root80() -> ConnectionInfo:
    return ConnectionInfo(
        host=_host("IT_MYSQL80_HOST"), port=3306, user="root",
        password=os.environ.get("IT_ROOT_PASSWORD", "root"), database="mysql",
    )


@pytest.fixture
def conn80() -> ConnectionInfo:
    return ConnectionInfo(
        host=_host("IT_MYSQL80_HOST"), port=3306, user="guardian", password=GUARDIAN_PASSWORD, database="shop"
    )


@pytest.fixture
def staging(root80):
    """A fresh pre-populated target database on mysql80 for the unprivileged user 'stager'."""
    database = f"staging_{uuid.uuid4().hex[:8]}"
    statements = [
        f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4",
        f"CREATE TABLE `{database}`.customers (id INT PRIMARY KEY, full_name VARCHAR(50))",
        f"CREATE TABLE `{database}`.extra_only (id INT PRIMARY KEY)",
        f"INSERT INTO `{database}`.extra_only VALUES (1)",
        "CREATE USER IF NOT EXISTS 'stager'@'%' IDENTIFIED BY 'st@ge\"r#1'",
        f"GRANT {STAGER_PRIVILEGES} ON `{database}`.* TO 'stager'@'%'",
    ]
    for statement in statements:
        root_query(root80, statement)
    yield ConnectionInfo(host=root80.host, port=3306, user="stager", password=STAGER_PASSWORD, database=database)
    root_query(root80, f"DROP DATABASE IF EXISTS `{database}`")
