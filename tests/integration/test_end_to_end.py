import gzip

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from db_guardian.app import create_app
from db_guardian.config import Settings
from db_guardian.container import build_container

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("tables, expected", [
    (["*"], {"customers": 3, "orders": 5, "hist_sales": 7}),
    (["hist_sales"], {"hist_sales": 7}),
])
def test_backup_lifecycle_through_the_api(conn, tmp_path, tables, expected):
    settings = Settings(
        encryption_key=Fernet.generate_key().decode(),
        signing_key="s" * 32,
        data_dir=tmp_path / "data",
        display_tz="America/Lima",
    )
    container = build_container(settings)
    with TestClient(create_app(container)) as client:
        source = client.post("/sources", json={
            "name": "shop", "host": conn.host, "port": conn.port, "user": conn.user,
            "password": conn.password, "database": conn.database,
            "tables": tables, "schedule": "0 3 * * *",
        }).json()

        check = client.post(f"/sources/{source['id']}/test-connection").json()
        assert check["ok"] is True, check

        queued = client.post(f"/sources/{source['id']}/backups")
        assert queued.status_code == 202
        container.queue.join()

        backup = client.get(f"/backups/{queued.json()['id']}").json()
        assert backup["status"] == "success", backup["error"]
        assert {t["name"]: t["row_count"] for t in backup["tables"]} == expected
        assert backup["total_rows"] == sum(expected.values())
        assert backup["file_size"] > 0

        url = client.post(f"/backups/{backup['id']}/download-url").json()["url"]
        download = client.get(url)
        assert download.status_code == 200
        text = gzip.decompress(download.content).decode()
        for name in expected:
            assert f"CREATE TABLE `{name}`" in text
