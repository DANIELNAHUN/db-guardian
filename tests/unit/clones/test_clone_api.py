import pytest
from sqlalchemy import func, select

from db_guardian.backups.recovery import INTERRUPTED, recover_interrupted
from db_guardian.clones.models import Clone, CloneStatus

KEY = {"X-Clone-Key": "test-clone-key"}


def target(**overrides):
    data = {"host": "staging.example", "port": 3306, "user": "stager", "password": "pw-123", "database": "intranet"}
    data.update(overrides)
    return data


@pytest.fixture
def ran(container):
    calls = []
    container.restorer = lambda conn, source, *, on_progress=None: calls.append((conn, source))
    container.clone_runner.restorer = container.restorer
    return calls


def clone_count(container):
    with container.session_factory() as s:
        return s.scalar(select(func.count()).select_from(Clone))


def test_execute_requires_key(client, backup, reader):
    assert client.post(f"/backups/{backup.id}/clones", json=target()).status_code == 401
    assert clone_count(client.app.state.container) == 0


def test_execute_returns_202_queued(client, backup, reader):
    r = client.post(f"/backups/{backup.id}/clones", json=target(), headers=KEY)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["backup_id"] == backup.id
    assert (body["target_host"], body["target_database"], body["target_user"]) == ("staging.example", "intranet", "stager")
    assert "password" not in body
    assert "pw-123" not in r.text


def test_execute_rejections_are_synchronous(client, backup, reader, container):
    r = client.post(f"/backups/{backup.id}/clones", json=target(host="127.0.0.1"), headers=KEY)
    assert r.status_code == 409
    assert clone_count(container) == 0


def test_execute_runs_on_worker(client, backup, reader, container, ran):
    container.queue.start()
    try:
        clone_id = client.post(f"/backups/{backup.id}/clones", json=target(), headers=KEY).json()["id"]
        container.queue.join()
    finally:
        container.queue.stop(timeout=5)
    body = client.get(f"/clones/{clone_id}").json()
    assert body["status"] == "success"
    assert body["progress"] == 1.0
    assert ran[0][0].password == "pw-123"


def test_history_list_and_detail(client, backup, reader):
    ids = [client.post(f"/backups/{backup.id}/clones", json=target(), headers=KEY).json()["id"] for _ in range(2)]
    listing = client.get("/clones").json()
    assert listing["total"] == 2
    assert [c["id"] for c in listing["items"]] == sorted(ids, reverse=True)
    assert client.get("/clones", params={"backup_id": backup.id + 1}).json()["total"] == 0
    assert client.get(f"/clones/{ids[0]}").json()["id"] == ids[0]
    assert client.get("/clones/999").status_code == 404


def test_recovery_fails_stale_clones(container, backup):
    with container.session_factory() as s:
        for status in (CloneStatus.QUEUED, CloneStatus.RUNNING, CloneStatus.SUCCESS):
            s.add(Clone(backup_id=backup.id, source_name="intranet", target_host="h", target_port=3306,
                        target_database="d", target_user="u", status=status))
        s.commit()
    recover_interrupted(container.session_factory, container.settings.backups_dir)
    with container.session_factory() as s:
        statuses = [(c.status, c.error) for c in s.scalars(select(Clone).order_by(Clone.id))]
    assert statuses == [("failed", INTERRUPTED), ("failed", INTERRUPTED), ("success", None)]
