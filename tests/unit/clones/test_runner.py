import json
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from db_guardian.clones.models import Clone, CloneStatus
from db_guardian.clones.runner import CloneRunner
from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.mysqlclient.restore import RestoreError

TARGET = ConnectionInfo(host="staging.example", port=3306, user="stager", password="pw-123", database="intranet")


class FakeRestorer:
    def __init__(self, container):
        self.container = container
        self.calls = []
        self.error = None
        self.steps = [i / 100 for i in range(1, 101)]
        self.persisted = []
        self.on_start = None
        self.on_end = None
        self.read_back = True

    def __call__(self, conn, source, *, on_progress=None):
        self.calls.append((conn, source))
        if self.on_start:
            self.on_start()
        for step in self.steps:
            if on_progress:
                on_progress(step)
            if self.read_back:
                with self.container.session_factory() as s:
                    self.persisted.append(s.get(Clone, self.clone_id).progress)
        if self.on_end:
            self.on_end()
        if self.error:
            raise self.error


@pytest.fixture
def restorer(container):
    return FakeRestorer(container)


@pytest.fixture
def run_clone(container, backup, reader, restorer):
    runner = CloneRunner(container.session_factory, reader, restorer)

    def _run() -> Clone:
        with container.session_factory() as s:
            clone = Clone(backup_id=backup.id, source_name="intranet", target_host=TARGET.host,
                          target_port=TARGET.port, target_database=TARGET.database, target_user=TARGET.user,
                          status=CloneStatus.QUEUED)
            s.add(clone)
            s.commit()
            restorer.clone_id = clone.id
        runner.run(clone.id, TARGET)
        with container.session_factory() as s:
            return s.get(Clone, clone.id)

    return _run


def test_clone_success_records(run_clone, restorer, backup):
    clone = run_clone()
    assert clone.status == CloneStatus.SUCCESS
    assert clone.progress == 1.0
    assert clone.tables == ["customers", "orders", "hist_sales"]
    assert clone.preview["counts"] == {
        "missing_in_target": 1, "structure_differs": 1, "only_in_target": 1, "matching": 1,
    }
    assert clone.preview["warnings"] == []
    assert clone.started_at is not None and clone.finished_at is not None
    assert clone.duration_seconds >= 0
    assert restorer.calls[0][0] == TARGET
    assert str(restorer.calls[0][1]) == backup.file_path
    row = {c.name: getattr(clone, c.name) for c in Clone.__table__.columns}
    assert "pw-123" not in json.dumps(row, default=str)


def test_clone_restore_failure(run_clone, restorer):
    restorer.error = RestoreError("mysql exited with code 1", "ERROR 1064 (42000) at line 3 ... pw-123")
    clone = run_clone()
    assert clone.status == CloneStatus.FAILED
    assert "1064" in clone.error
    assert "pw-123" not in clone.error
    assert clone.finished_at is not None


def test_clone_target_unreachable(run_clone, reader, restorer):
    reader.error = OSError("connection refused")
    clone = run_clone()
    assert clone.status == CloneStatus.FAILED
    assert "connection refused" in clone.error
    assert restorer.calls == []


def test_failed_clone_still_records_tables(run_clone, restorer):
    restorer.error = RestoreError("mysql exited with code 1", "ERROR 1064")
    assert run_clone().tables == ["customers", "orders", "hist_sales"]


def test_progress_write_failure_does_not_abort_restore(run_clone, restorer, monkeypatch):
    failing = {"on": False}
    real_commit = Session.commit

    def commit(self):
        if failing["on"]:
            raise OperationalError("UPDATE clones", {}, Exception("database is locked"))
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", commit)
    restorer.on_start = lambda: failing.update(on=True)
    restorer.steps = [0.1, 0.5]
    restorer.error = None
    original_call = restorer.__class__.__call__

    def call_then_recover(self, *args, **kwargs):
        try:
            original_call(self, *args, **kwargs)
        finally:
            failing["on"] = False

    monkeypatch.setattr(restorer.__class__, "__call__", call_then_recover)
    clone = run_clone()
    assert clone.status == CloneStatus.SUCCESS
    assert clone.progress == 1.0


def test_progress_survives_a_broken_session(run_clone, restorer, monkeypatch):
    broken = {"on": False}

    def failing(real):
        def call(self):
            if broken["on"]:
                raise OperationalError("UPDATE clones", {}, Exception("disk I/O error"))
            return real(self)
        return call

    monkeypatch.setattr(Session, "commit", failing(Session.commit))
    monkeypatch.setattr(Session, "rollback", failing(Session.rollback))
    restorer.steps = [0.1, 0.5, 0.9]
    restorer.read_back = False
    restorer.on_start = lambda: broken.update(on=True)
    restorer.on_end = lambda: broken.update(on=False)
    clone = run_clone()
    assert clone.status == CloneStatus.SUCCESS
    assert clone.progress == 1.0


def test_progress_never_reads_back_through_a_failing_database(run_clone, restorer, container):
    """Every statement fails while the database is down; the real rollback then expires the
    clone, so reading clone.progress would issue a SELECT that fails outside the saver's guard."""
    down = {"on": False, "refused": 0}

    def refuse(*_args, **_kwargs):
        if down["on"]:
            down["refused"] += 1
            raise OperationalError("statement", {}, Exception("disk I/O error"))

    event.listen(container.engine, "before_cursor_execute", refuse)
    try:
        restorer.steps = [0.1, 0.5, 0.9]
        restorer.read_back = False
        restorer.on_start = lambda: down.update(on=True)
        restorer.on_end = lambda: down.update(on=False)
        clone = run_clone()
    finally:
        event.remove(container.engine, "before_cursor_execute", refuse)
    assert down["refused"] >= len(restorer.steps)  # every progress write hit the outage
    assert clone.status == CloneStatus.SUCCESS
    assert clone.progress == 1.0


def test_backup_file_gone_before_run(run_clone, restorer, backup):
    Path(backup.file_path).unlink()
    clone = run_clone()
    assert clone.status == CloneStatus.FAILED
    assert clone.error == "backup file is no longer available (removed by retention?)"
    assert restorer.calls == []


def test_clone_progress_persisted_in_steps(run_clone, restorer):
    run_clone()
    seen = [p for i, p in enumerate(restorer.persisted) if i == 0 or p != restorer.persisted[i - 1]]
    assert seen == sorted(seen)
    assert len(seen) <= 22
    assert all(b - a >= 0.05 - 1e-9 for a, b in zip(seen[1:], seen[2:]))
