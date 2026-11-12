from pathlib import Path

import pytest

from db_guardian.backups.models import Backup, BackupStatus, BackupTrigger, FileState
from db_guardian.backups.runner import BackupRunner
from db_guardian.mysqlclient.dump import DumpError
from db_guardian.mysqlclient.metadata import MissingTablesError, SnapshotMeta, TableMeta
from db_guardian.sources.schemas import SourceCreate
from db_guardian.sources.service import SourceService


class FakeMeta:
    def __init__(self):
        self.calls = []
        self.error = None

    def __call__(self, conn, tables):
        self.calls.append((conn, tables))
        if self.error:
            raise self.error
        return SnapshotMeta(
            server_version="5.7.41-log",
            tables=[
                TableMeta("hist_a", 10, "CREATE TABLE `hist_a` (...)"),
                TableMeta("hist_b", 20, "CREATE TABLE `hist_b` (...)"),
            ],
        )


class FakeDumper:
    def __init__(self):
        self.calls = []
        self.fail_after_bytes = None

    def __call__(self, conn, tables, server_version, dest: Path) -> int:
        self.calls.append((conn, tables, server_version, dest))
        dest.write_bytes(b"x" * 100)
        if self.fail_after_bytes is not None:
            raise DumpError("mysqldump exited with code 2", "No space left on device")
        return dest.stat().st_size


@pytest.fixture
def fake_meta():
    return FakeMeta()


@pytest.fixture
def fake_dumper():
    return FakeDumper()


@pytest.fixture
def source_id(container):
    with container.session_factory() as s:
        source = SourceService(s, container.box).create(SourceCreate(
            name="etl", host="h", user="u", password="pw", database="etl",
            tables=["hist_a", "hist_b"], schedule="0 3 * * *", retention=3,
        ))
        return source.id


@pytest.fixture
def runner(container, fake_meta, fake_dumper):
    return BackupRunner(
        container.session_factory, container.box, container.settings.backups_dir, fake_dumper, fake_meta
    )


@pytest.fixture
def run_one(container, runner, source_id):
    def _run() -> Backup:
        with container.session_factory() as s:
            backup = Backup(source_id=source_id, source_name="etl", trigger=BackupTrigger.MANUAL,
                            status=BackupStatus.QUEUED)
            s.add(backup)
            s.commit()
            backup_id = backup.id
        runner.run(backup_id)
        with container.session_factory() as s:
            backup = s.get(Backup, backup_id)
            backup.tables  # load while the session is open
            return backup
    return _run


def test_success_records_everything(run_one, fake_meta, fake_dumper):
    b = run_one()
    assert b.status == BackupStatus.SUCCESS
    assert b.file_state == FileState.PRESENT
    assert Path(b.file_path).exists()
    assert Path(b.file_path).name.endswith(f"_{b.id}.sql.gz")
    assert b.file_size == 100
    assert b.server_version == "5.7.41-log"
    assert b.started_at is not None and b.finished_at is not None
    assert b.duration_seconds >= 0
    assert b.error is None
    assert {(t.name, t.row_count, t.create_sql) for t in b.tables} == {
        ("hist_a", 10, "CREATE TABLE `hist_a` (...)"),
        ("hist_b", 20, "CREATE TABLE `hist_b` (...)"),
    }
    conn, tables = fake_meta.calls[0]
    assert conn.password == "pw" and tables == ["hist_a", "hist_b"]
    assert fake_dumper.calls[0][2] == "5.7.41-log"


def test_dump_failure_midstream_cleans_up_and_keeps_previous(run_one, fake_dumper, container):
    good = run_one()
    fake_dumper.fail_after_bytes = 100
    bad = run_one()
    assert bad.status == BackupStatus.FAILED
    assert "No space left on device" in bad.error
    assert bad.file_state == FileState.NONE
    assert bad.finished_at is not None
    files = list(container.settings.backups_dir.rglob("*"))
    assert not any(p.suffix == ".tmp" for p in files)
    assert [p for p in files if p.is_file()] == [Path(good.file_path)]


def test_missing_tables_fails_with_names(run_one, fake_meta, fake_dumper):
    fake_meta.error = MissingTablesError(["hist_x"])
    b = run_one()
    assert b.status == BackupStatus.FAILED
    assert "hist_x" in b.error
    assert fake_dumper.calls == []


def test_unexpected_error_does_not_raise(run_one, fake_meta):
    fake_meta.error = OSError("connection refused")
    b = run_one()
    assert b.status == BackupStatus.FAILED
    assert "connection refused" in b.error


def test_success_applies_retention(run_one, container):
    backups = [run_one() for _ in range(4)]
    with container.session_factory() as s:
        oldest = s.get(Backup, backups[0].id)
        assert oldest.file_state == FileState.DELETED_BY_RETENTION
        assert not Path(oldest.file_path).exists()
        assert all(s.get(Backup, b.id).file_state == FileState.PRESENT for b in backups[1:])


def test_retention_failure_keeps_successful_backup(run_one, monkeypatch):
    from db_guardian.backups import runner as runner_module

    def broken_retention(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(runner_module, "apply_retention", broken_retention)
    b = run_one()
    assert b.status == BackupStatus.SUCCESS
    assert b.file_state == FileState.PRESENT
    assert Path(b.file_path).exists()
