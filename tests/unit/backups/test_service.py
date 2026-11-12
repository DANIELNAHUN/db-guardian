import pytest
from sqlalchemy import func, select

from db_guardian.backups.models import Backup, BackupStatus, BackupTrigger
from db_guardian.backups.service import SourceUnavailable, enqueue_backup


def payload(**overrides):
    data = {
        "name": "intranet", "host": "10.0.0.5", "user": "backup", "password": "secret",
        "database": "intranet", "tables": ["*"], "schedule": "0 3 * * *",
    }
    data.update(overrides)
    return data


@pytest.fixture
def source_id(client):
    return client.post("/sources", json=payload()).json()["id"]


def queued_count(container):
    with container.session_factory() as s:
        return s.scalar(select(func.count()).select_from(Backup).where(Backup.status == BackupStatus.QUEUED))


def test_manual_trigger_twice_returns_same_queued(client, container, source_id):
    # The queue is not started (start_background=False), so the first job stays pending.
    r1 = client.post(f"/sources/{source_id}/backups")
    r2 = client.post(f"/sources/{source_id}/backups")
    assert r1.status_code == 202
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["status"] == "queued"
    assert r1.json()["trigger"] == "manual"
    assert queued_count(container) == 1


def test_scheduled_enqueue_reuses_queued_manual(client, container, source_id):
    first = client.post(f"/sources/{source_id}/backups").json()
    backup, created = enqueue_backup(container, source_id, BackupTrigger.SCHEDULED)
    assert created is False
    assert backup.id == first["id"]


def test_trigger_disabled_source_409(client, source_id):
    client.patch(f"/sources/{source_id}", json={"enabled": False})
    assert client.post(f"/sources/{source_id}/backups").status_code == 409


def test_trigger_deleted_or_unknown_source(client, container, source_id):
    client.delete(f"/sources/{source_id}")
    assert client.post(f"/sources/{source_id}/backups").status_code == 404
    with pytest.raises(SourceUnavailable):
        enqueue_backup(container, source_id, BackupTrigger.SCHEDULED)


def test_enqueued_job_runs_on_worker(client, container, source_id, monkeypatch):
    ran = []
    monkeypatch.setattr(container.runner, "run", lambda backup_id: ran.append(backup_id))
    container.queue.start()
    try:
        backup = client.post(f"/sources/{source_id}/backups").json()
        container.queue.join()
    finally:
        container.queue.stop(timeout=5)
    assert ran == [backup["id"]]


def test_test_connection_ok(client, container, source_id):
    container.version_reader = lambda conn: "8.0.46"
    body = client.post(f"/sources/{source_id}/test-connection").json()
    assert body == {"ok": True, "server_version": "8.0.46", "error": None}


def test_test_connection_reports_error_without_password(client, container, source_id):
    def failing(conn):
        raise OSError(f"(1045) Access denied for user '{conn.user}'@'10.0.0.9'")

    container.version_reader = failing
    r = client.post(f"/sources/{source_id}/test-connection")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "Access denied" in body["error"]
    assert "secret" not in body["error"]
