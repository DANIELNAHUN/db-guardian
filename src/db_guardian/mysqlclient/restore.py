"""Restore a .sql.gz backup into a target database through the mysql client."""

import gzip
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.mysqlclient.dump import DUMP_WORKDIR_PREFIX, STDERR_TAIL_CHARS, write_option_file

MYSQL = "mysql"
WRITE_BATCH_BYTES = 1024 * 1024

# DEFINER=`user`@`host` (backtick-quoted, as mysqldump writes it).
_DEFINER = re.compile(rb"DEFINER=`[^`]*`@`[^`]*`")

Restorer = Callable[..., None]


class RestoreError(RuntimeError):
    def __init__(self, message: str, stderr_tail: str = "") -> None:
        super().__init__(f"{message}: {stderr_tail}" if stderr_tail else message)
        self.stderr_tail = stderr_tail


def strip_definers(line: bytes) -> bytes:
    """Remove DEFINER clauses so the target user becomes the definer of views/triggers/routines."""
    if b"DEFINER=`" not in line:
        return line
    return _DEFINER.sub(b"", line)


def _spawn(args: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(args, **kwargs)


def restore_gzip(
    conn: ConnectionInfo,
    source: Path,
    *,
    on_progress: Callable[[float], None] | None = None,
    timeout_seconds: float | None = None,
) -> None:
    """Stream `source` into `conn.database` with TLS required.

    `on_progress` receives the fraction of the compressed file consumed, at most
    once per percent, ending with 1.0. Raises RestoreError on any failure.
    """
    total = max(source.stat().st_size, 1)
    workdir = Path(tempfile.mkdtemp(prefix=DUMP_WORKDIR_PREFIX))
    try:
        option_file = write_option_file(conn, workdir, ssl_mode="REQUIRED")
        args = [MYSQL, f"--defaults-extra-file={option_file}", "--default-character-set=utf8mb4", conn.database]
        timed_out = threading.Event()
        with open(workdir / "stderr.log", "w+b") as stderr, open(source, "rb") as raw:
            with _spawn(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr, bufsize=0) as proc:
                assert proc.stdin is not None
                watchdog = None
                if timeout_seconds:
                    def _expire() -> None:
                        timed_out.set()
                        proc.kill()

                    watchdog = threading.Timer(timeout_seconds, _expire)
                    watchdog.daemon = True
                    watchdog.start()
                try:
                    _feed(gzip.GzipFile(fileobj=raw), raw, total, proc.stdin, on_progress)
                except BrokenPipeError:
                    pass  # mysql exited early; its exit code and stderr explain why
                except BaseException:
                    proc.kill()
                    raise
                finally:
                    if watchdog:
                        watchdog.cancel()
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                code = proc.wait()
            stderr.seek(0)
            tail = stderr.read().decode(errors="replace")[-STDERR_TAIL_CHARS:].strip()
        if timed_out.is_set():
            raise RestoreError(f"mysql restore timed out after {timeout_seconds:g}s and was killed", tail)
        if code != 0:
            raise RestoreError(f"mysql exited with code {code}", tail)
        if on_progress:
            on_progress(1.0)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _feed(lines, raw, total: int, stdin, on_progress: Callable[[float], None] | None) -> None:
    batch: list[bytes] = []
    size = 0
    last_percent = -1
    for line in lines:
        batch.append(strip_definers(line))
        size += len(line)
        if size >= WRITE_BATCH_BYTES:
            stdin.write(b"".join(batch))
            batch, size = [], 0
            if on_progress:
                percent = min(int(raw.tell() * 100 / total), 99)
                if percent > last_percent:
                    last_percent = percent
                    on_progress(percent / 100)
    if batch:
        stdin.write(b"".join(batch))
