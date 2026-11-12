def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_database_file_created(client, settings):
    assert settings.db_path.exists()


def test_background_startup_recovers_and_shuts_down(container):
    from fastapi.testclient import TestClient

    from db_guardian.app import create_app
    from db_guardian.backups.models import Backup, BackupStatus, BackupTrigger
    from db_guardian.sources.models import Source

    with container.session_factory() as s:
        source = Source(name="intranet", host="h", port=3306, user="u", password_enc="x",
                        database="d", tables=["*"], schedule="0 3 * * *", retention=3)
        s.add(source)
        s.flush()
        stale = Backup(source_id=source.id, source_name="intranet", trigger=BackupTrigger.SCHEDULED,
                       status=BackupStatus.RUNNING)
        s.add(stale)
        s.commit()
        stale_id, source_id = stale.id, source.id

    with TestClient(create_app(container)) as c:
        assert c.get("/health").status_code == 200
        assert container.scheduler.get_job(source_id) is not None
        assert c.get(f"/backups/{stale_id}").json()["status"] == "failed"
