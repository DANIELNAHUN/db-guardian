import json

import pytest

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

    def __call__(self, conn, source, *, on_progress=None):
        self.calls.append((conn, source))
        for step in self.steps:
            if on_progress:
                on_progress(step)
            with self.container.session_factory() as s:
                self.persisted.append(s.get(Clone, self.clone_id).progress)
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


def test_clone_progress_persisted_in_steps(run_clone, restorer):
    run_clone()
    seen = [p for i, p in enumerate(restorer.persisted) if i == 0 or p != restorer.persisted[i - 1]]
    assert seen == sorted(seen)
    assert len(seen) <= 22
    assert all(b - a >= 0.05 - 1e-9 for a, b in zip(seen[1:], seen[2:]))
