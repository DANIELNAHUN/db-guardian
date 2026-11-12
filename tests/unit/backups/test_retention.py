from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from db_guardian.backups.models import Backup, BackupStatus, BackupTrigger, FileState
from db_guardian.backups.retention import apply_retention
from db_guardian.sources.models import Source


@pytest.fixture
def session(container):
    with container.session_factory() as s:
        yield s


def make_source(session, name):
    source = Source(name=name, host="h", port=3306, user="u", password_enc="x", database="d",
                    tables=["*"], schedule="0 3 * * *", retention=3)
    session.add(source)
    session.flush()
    return source


def make_backup(session, source, tmp_path, i, status=BackupStatus.SUCCESS, with_file=True):
    path = tmp_path / f"{source.name}_{i}.sql.gz"
    if with_file:
        path.write_bytes(b"data")
    backup = Backup(
        source_id=source.id, source_name=source.name, trigger=BackupTrigger.SCHEDULED, status=status,
        finished_at=datetime(2026, 11, 1) + timedelta(days=i),
        file_path=str(path) if status == BackupStatus.SUCCESS else None,
        file_state=FileState.PRESENT if status == BackupStatus.SUCCESS else FileState.NONE,
    )
    session.add(backup)
    session.flush()
    return backup


def test_retention_keeps_n_successful(session, tmp_path):
    source = make_source(session, "intranet")
    successes = [make_backup(session, source, tmp_path, i) for i in range(5)]
    failures = [make_backup(session, source, tmp_path, 10 + i, BackupStatus.FAILED) for i in range(2)]

    deleted = apply_retention(session, source.id, keep=3)

    assert sorted(deleted) == sorted(b.id for b in successes[:2])
    for b in successes[:2]:
        assert b.file_state == FileState.DELETED_BY_RETENTION
        assert not Path(b.file_path).exists()
    for b in successes[2:]:
        assert b.file_state == FileState.PRESENT
        assert Path(b.file_path).exists()
    assert all(b.status == BackupStatus.FAILED for b in failures)
    assert session.scalar(select(func.count()).select_from(Backup)) == 7


def test_failures_never_push_out_good_backups(session, tmp_path):
    source = make_source(session, "intranet")
    successes = [make_backup(session, source, tmp_path, i) for i in range(3)]
    for i in range(5):
        make_backup(session, source, tmp_path, 10 + i, BackupStatus.FAILED)
    assert apply_retention(session, source.id, keep=3) == []
    assert all(b.file_state == FileState.PRESENT for b in successes)


def test_retention_ignores_other_sources(session, tmp_path):
    a = make_source(session, "a")
    b = make_source(session, "b")
    for i in range(3):
        make_backup(session, a, tmp_path, i)
    others = [make_backup(session, b, tmp_path, 100 + i) for i in range(3)]
    assert apply_retention(session, a.id, keep=1) != []
    assert all(o.file_state == FileState.PRESENT for o in others)


def test_retention_tolerates_already_missing_file(session, tmp_path):
    source = make_source(session, "intranet")
    old = make_backup(session, source, tmp_path, 0, with_file=False)
    make_backup(session, source, tmp_path, 1)
    assert apply_retention(session, source.id, keep=1) == [old.id]
    assert old.file_state == FileState.DELETED_BY_RETENTION
