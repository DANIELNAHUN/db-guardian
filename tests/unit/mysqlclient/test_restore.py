import gzip
import sys
import time
from pathlib import Path

import pytest

from db_guardian.mysqlclient import restore as restore_module
from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.mysqlclient.dump import write_option_file
from db_guardian.mysqlclient.restore import RestoreError, restore_gzip, strip_definers

CONN = ConnectionInfo(host="staging.example", port=3306, user="stager", password="pw", database="staging")

FAKE_MYSQL = """
import os, shutil, sys, time
mode = os.environ.get("FAKE_MODE", "copy")
if mode == "fail":
    sys.stdin.buffer.read()
    sys.stderr.write("ERROR 1064 (42000) at line 3: You have an error in your SQL syntax\\n")
    sys.exit(1)
if mode == "sleep":
    time.sleep(30)
if mode == "hang_after_read":
    sys.stdin.buffer.read()
    time.sleep(30)
with open(os.environ["FAKE_OUT"], "wb") as out:
    shutil.copyfileobj(sys.stdin.buffer, out)
with open(os.environ["FAKE_OUT"] + ".args", "w") as out:
    out.write("\\n".join(sys.argv[1:]))
"""


def test_strip_definers():
    view = b"/*!50013 DEFINER=`root`@`localhost` SQL SECURITY DEFINER */ VIEW `v` AS select 1;\n"
    assert strip_definers(view) == b"/*!50013  SQL SECURITY DEFINER */ VIEW `v` AS select 1;\n"
    assert strip_definers(b"CREATE DEFINER=`app`@`%` PROCEDURE `p`()") == b"CREATE  PROCEDURE `p`()"
    text = b"INSERT INTO t VALUES ('DEFINER is text');"
    assert strip_definers(text) == text


def test_option_file_ssl_mode(tmp_path):
    (tmp_path / "a").mkdir()
    with_ssl = write_option_file(CONN, tmp_path / "a", ssl_mode="REQUIRED")
    assert "ssl-mode=REQUIRED\n" in with_ssl.read_text()
    (tmp_path / "b").mkdir()
    assert "ssl-mode" not in write_option_file(CONN, tmp_path / "b").read_text()


@pytest.fixture
def backup_file(tmp_path) -> Path:
    path = tmp_path / "backup.sql.gz"
    lines = [b"/*!50013 DEFINER=`root`@`localhost` SQL SECURITY DEFINER */\n"]
    lines += [f"INSERT INTO t VALUES ({i}, 'row {i} padding padding padding');\n".encode() for i in range(20000)]
    lines += [b"CREATE DEFINER=`root`@`localhost` PROCEDURE `p`() SELECT 1;\n"]
    with gzip.open(path, "wb") as fh:
        fh.writelines(lines)
    return path


@pytest.fixture
def fake_mysql(tmp_path, monkeypatch):
    script = tmp_path / "fake_mysql.py"
    script.write_text(FAKE_MYSQL)
    out = tmp_path / "received.sql"
    monkeypatch.setenv("FAKE_OUT", str(out))
    seen = {}
    real_spawn = restore_module._spawn

    def spawn(args, **kwargs):
        seen["args"] = args
        return real_spawn([sys.executable, str(script), *args[1:]], **kwargs)

    monkeypatch.setattr(restore_module, "_spawn", spawn)
    return {"out": out, "seen": seen, "env": monkeypatch}


def option_file_from(args) -> Path:
    return Path(next(a for a in args if a.startswith("--defaults-extra-file=")).split("=", 1)[1])


def test_restore_feeds_stripped_sql_and_reports_progress(fake_mysql, backup_file):
    progress = []
    restore_gzip(CONN, backup_file, on_progress=progress.append)
    received = fake_mysql["out"].read_bytes()
    assert b"DEFINER=" not in received
    assert received.count(b"INSERT INTO t") == 20000
    assert progress[-1] == 1.0
    assert progress == sorted(progress)
    assert len(progress) <= 101
    args = Path(str(fake_mysql["out"]) + ".args").read_text().splitlines()
    assert args[-1] == "--database=staging"
    assert "--default-character-set=utf8mb4" in args


def test_restore_option_file_requires_tls_and_is_removed(fake_mysql, backup_file):
    seen_text = {}
    real_spawn = restore_module._spawn

    def capture(args, **kwargs):
        seen_text["opt"] = option_file_from(args).read_text()
        return real_spawn(args, **kwargs)

    fake_mysql["env"].setattr(restore_module, "_spawn", capture)
    restore_gzip(CONN, backup_file)
    assert "ssl-mode=REQUIRED" in seen_text["opt"]
    assert not option_file_from(fake_mysql["seen"]["args"]).exists()


def test_restore_failure_raises_with_stderr(fake_mysql, backup_file):
    fake_mysql["env"].setenv("FAKE_MODE", "fail")
    with pytest.raises(RestoreError) as exc_info:
        restore_gzip(CONN, backup_file)
    assert "1064" in exc_info.value.stderr_tail
    assert not option_file_from(fake_mysql["seen"]["args"]).exists()


def test_restore_timeout_kills(fake_mysql, backup_file):
    fake_mysql["env"].setenv("FAKE_MODE", "sleep")
    start = time.monotonic()
    with pytest.raises(RestoreError, match="timed out after 1s"):
        restore_gzip(CONN, backup_file, timeout_seconds=1)
    assert time.monotonic() - start < 15


def test_strip_definers_leaves_insert_rows_alone():
    row = b"INSERT INTO `notes` VALUES (1,'see DEFINER=`root`@`localhost` in the view');\n"
    assert strip_definers(row) == row


def test_restore_passes_database_as_option_value(fake_mysql, backup_file):
    dangerous = ConnectionInfo(host="h", port=3306, user="u", password="pw", database="--local-infile=1")
    restore_gzip(dangerous, backup_file)
    args = fake_mysql["seen"]["args"]
    assert "--database=--local-infile=1" in args
    assert "--local-infile=1" not in args


def test_restore_timeout_covers_final_statements(fake_mysql, backup_file):
    fake_mysql["env"].setenv("FAKE_MODE", "hang_after_read")
    start = time.monotonic()
    with pytest.raises(RestoreError, match="timed out after 1s"):
        restore_gzip(CONN, backup_file, timeout_seconds=1)
    assert time.monotonic() - start < 15
