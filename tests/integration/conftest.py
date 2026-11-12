import gzip
import os
import shutil
import subprocess
import tempfile
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
