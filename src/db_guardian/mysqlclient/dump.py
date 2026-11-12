"""Build and run mysqldump, streaming its output into a gzip file."""

import gzip
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from db_guardian.mysqlclient.connection import ConnectionInfo

MYSQLDUMP = "mysqldump"
CHUNK_SIZE = 1024 * 1024
STDERR_TAIL_CHARS = 2000
DUMP_WORKDIR_PREFIX = "guardian-dump-"

Dumper = Callable[[ConnectionInfo, list[str] | None, str, Path], int]


class DumpError(RuntimeError):
    def __init__(self, message: str, stderr_tail: str = "") -> None:
        super().__init__(f"{message}: {stderr_tail}" if stderr_tail else message)
        self.stderr_tail = stderr_tail


def parse_version(version: str) -> tuple[int, int]:
    match = re.match(r"(\d+)\.(\d+)", version)
    if not match:
        raise ValueError(f"unrecognized server version {version!r}")
    return int(match.group(1)), int(match.group(2))


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_option_file(conn: ConnectionInfo, directory: Path) -> Path:
    """Write a [client] option file so the password never appears in argv."""
    path = directory / "client.cnf"
    content = (
        "[client]\n"
        f"user={_quote(conn.user)}\n"
        f"password={_quote(conn.password)}\n"
        f"host={_quote(conn.host)}\n"
        f"port={conn.port}\n"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="\n") as fh:
        fh.write(content)
    return path


def build_dump_args(
    conn: ConnectionInfo, tables: list[str] | None, server_version: str, option_file: Path
) -> list[str]:
    args = [
        MYSQLDUMP,
        f"--defaults-extra-file={option_file}",  # must be the first option
        "--single-transaction",
        "--quick",
        "--set-gtid-purged=OFF",
        "--no-tablespaces",
        "--add-drop-table",
        "--default-character-set=utf8mb4",
    ]
    if parse_version(server_version) < (8, 0):
        args.append("--column-statistics=0")
    if tables is None:
        args += ["--routines", "--triggers", "--events", conn.database]
    else:
        args += ["--triggers", conn.database, *tables]
    return args


def _spawn(args: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(args, **kwargs)


def dump_to_gzip(
    conn: ConnectionInfo,
    tables: list[str] | None,
    server_version: str,
    dest: Path,
    timeout_seconds: float | None = None,
) -> int:
    """Run mysqldump into `dest` (gzip). Returns the compressed size.

    A dump running longer than `timeout_seconds` is killed (e.g. a silent network
    drop). Leaves `dest` in place on failure; the caller owns cleanup.
    """
    workdir = Path(tempfile.mkdtemp(prefix=DUMP_WORKDIR_PREFIX))
    try:
        option_file = write_option_file(conn, workdir)
        args = build_dump_args(conn, tables, server_version, option_file)
        # stderr goes to a file so a chatty mysqldump can never block on a full pipe.
        with open(workdir / "stderr.log", "w+b") as stderr, gzip.open(dest, "wb", compresslevel=6) as out:
            with _spawn(args, stdout=subprocess.PIPE, stderr=stderr) as proc:
                assert proc.stdout is not None
                timed_out = threading.Event()
                watchdog = None
                if timeout_seconds:
                    def _expire() -> None:
                        timed_out.set()
                        proc.kill()

                    watchdog = threading.Timer(timeout_seconds, _expire)
                    watchdog.daemon = True
                    watchdog.start()
                try:
                    while chunk := proc.stdout.read(CHUNK_SIZE):
                        out.write(chunk)
                except BaseException:
                    proc.kill()  # e.g. disk full while writing: stop mysqldump too
                    raise
                finally:
                    if watchdog:
                        watchdog.cancel()
                code = proc.wait()
            stderr.seek(0)
            tail = stderr.read().decode(errors="replace")[-STDERR_TAIL_CHARS:].strip()
        if timed_out.is_set():
            raise DumpError(f"mysqldump timed out after {timeout_seconds:g}s and was killed", tail)
        if code != 0:
            raise DumpError(f"mysqldump exited with code {code}", tail)
        return dest.stat().st_size
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
