import gzip
import sys
from pathlib import Path

import pytest

from db_guardian.mysqlclient import dump as dump_module
from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.mysqlclient.dump import (
    DumpError,
    build_dump_args,
    dump_to_gzip,
    parse_version,
    write_option_file,
)

OPT = Path("/tmp/opt.cnf")


def conn(password="s3cr3t", database="intranet"):
    return ConnectionInfo(host="10.0.0.5", port=3306, user="backup", password=password, database=database)


def test_parse_version():
    assert parse_version("5.7.41-log") == (5, 7)
    assert parse_version("8.0.46") == (8, 0)
    assert parse_version("8.4.3-commercial") == (8, 4)


def test_full_dump_args():
    args = build_dump_args(conn(), None, "8.0.46", OPT)
    assert args[:2] == ["mysqldump", f"--defaults-extra-file={OPT}"]
    assert {
        "--single-transaction", "--quick", "--set-gtid-purged=OFF", "--no-tablespaces",
        "--add-drop-table", "--default-character-set=utf8mb4",
        "--routines", "--triggers", "--events",
    } <= set(args)
    assert args[-1] == "intranet"
    assert "--column-statistics=0" not in args


def test_table_list_args_on_57():
    args = build_dump_args(conn(database="etl"), ["hist_a", "hist_b"], "5.7.41-log", OPT)
    assert args[-3:] == ["etl", "hist_a", "hist_b"]
    assert "--triggers" in args
    assert "--routines" not in args and "--events" not in args
    assert "--column-statistics=0" in args


def test_password_not_in_args():
    assert all("s3cr3t" not in a for a in build_dump_args(conn(), None, "8.0.46", OPT))


def unescape_option_value(raw: str) -> str:
    """Reverse of the option-file quoting: strip quotes, undo backslash escapes."""
    assert raw.startswith('"') and raw.endswith('"')
    out, i, body = [], 0, raw[1:-1]
    while i < len(body):
        if body[i] == "\\":
            out.append(body[i + 1])
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def read_option(path: Path, key: str) -> str:
    for line in path.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line[len(key) + 1:]
    raise AssertionError(f"{key} not found")


@pytest.mark.parametrize("pw", ["a#b", 'q"uote', "back\\slash", "with space", "eq=ual", "x'y"])
def test_option_file_roundtrips_password(tmp_path, pw):
    path = write_option_file(conn(pw), tmp_path)
    text = path.read_text()
    assert text.startswith("[client]\n")
    assert unescape_option_value(read_option(path, "password")) == pw
    assert unescape_option_value(read_option(path, "user")) == "backup"
    assert read_option(path, "port") == "3306"


FAKE_DUMP = """
import sys
args = sys.argv[1:]
if "--fail" in open(args[0].split("=", 1)[1]).read():
    sys.stderr.write("mysqldump: Got error: 1045: Access denied for user 'backup'\\n")
    sys.exit(2)
sys.stdout.write("-- dump of " + args[-1] + "\\n" + "INSERT INTO t VALUES (1);\\n" * 1000)
"""


@pytest.fixture
def fake_mysqldump(tmp_path, monkeypatch):
    script = tmp_path / "fake_mysqldump.py"
    script.write_text(FAKE_DUMP)
    seen = {}
    real_spawn = dump_module._spawn

    def spawn(args, **kwargs):
        seen["args"] = args
        return real_spawn([sys.executable, str(script), *args[1:]], **kwargs)

    monkeypatch.setattr(dump_module, "_spawn", spawn)
    return seen


def option_file_from(args):
    return Path(next(a for a in args if a.startswith("--defaults-extra-file=")).split("=", 1)[1])


def test_dump_to_gzip_streams_output(tmp_path, fake_mysqldump):
    dest = tmp_path / "out.sql.gz"
    size = dump_to_gzip(conn(), None, "8.0.46", dest)
    assert size == dest.stat().st_size > 0
    text = gzip.decompress(dest.read_bytes()).decode()
    assert text.startswith("-- dump of intranet")
    assert text.count("INSERT INTO t") == 1000
    assert not option_file_from(fake_mysqldump["args"]).exists()


def test_dump_to_gzip_raises_and_removes_option_file(tmp_path, fake_mysqldump):
    with pytest.raises(DumpError) as exc_info:
        dump_to_gzip(conn(password="--fail"), None, "8.0.46", tmp_path / "out.sql.gz")
    assert "Access denied" in exc_info.value.stderr_tail
    assert "--fail" not in str(exc_info.value)
    assert not option_file_from(fake_mysqldump["args"]).exists()
