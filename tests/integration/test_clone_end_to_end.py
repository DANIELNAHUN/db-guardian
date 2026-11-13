import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from db_guardian.app import create_app
from db_guardian.config import Settings
from db_guardian.container import build_container

pytestmark = pytest.mark.integration

KEY = {"X-Clone-Key": "it-clone-key"}


def test_clone_lifecycle_through_the_api(conn80, staging, sql80, tmp_path):
    settings = Settings(
        encryption_key=Fernet.generate_key().decode(),
        signing_key="s" * 32,
        clone_key="it-clone-key",
        data_dir=tmp_path / "data",
        display_tz="America/Lima",
    )
    container = build_container(settings)
    target = {
        "host": staging.host, "port": staging.port, "user": staging.user,
        "password": staging.password, "database": staging.database,
    }
    with TestClient(create_app(container)) as client:
        source = client.post("/sources", json={
            "name": "shop", "host": conn80.host, "port": conn80.port, "user": conn80.user,
            "password": conn80.password, "database": conn80.database,
            "tables": ["*"], "schedule": "0 3 * * *",
        }).json()
        backup_id = client.post(f"/sources/{source['id']}/backups").json()["id"]
        container.queue.join()
        assert client.get(f"/backups/{backup_id}").json()["status"] == "success"

        before = client.post(f"/backups/{backup_id}/clone-preview", json=target, headers=KEY)
        assert before.status_code == 200, before.text
        report = before.json()
        assert {t["name"] for t in report["missing_in_target"]} == {"orders", "hist_sales"}
        assert [t["name"] for t in report["structure_differs"]] == ["customers"]
        assert report["only_in_target"] == ["extra_only"]
        assert report["warnings"] == []

        started = client.post(f"/backups/{backup_id}/clones", json=target, headers=KEY)
        assert started.status_code == 202, started.text
        container.queue.join()

        clone = client.get(f"/clones/{started.json()['id']}").json()
        assert clone["status"] == "success", clone["error"]
        assert clone["progress"] == 1.0
        assert clone["preview"]["counts"]["only_in_target"] == 1

        db = staging.database
        counts = {t: sql80(f"SELECT COUNT(*) FROM `{t}`", db)[0][0] for t in ("customers", "orders", "hist_sales")}
        assert counts == {"customers": 3, "orders": 5, "hist_sales": 7}
        assert sql80("SELECT COUNT(*) FROM extra_only", db)[0][0] == 1

        after = client.post(f"/backups/{backup_id}/clone-preview", json=target, headers=KEY).json()
        assert after["missing_in_target"] == []
        assert after["structure_differs"] == []
        assert after["only_in_target"] == ["extra_only"]
