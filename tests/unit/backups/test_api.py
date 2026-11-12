import json
from datetime import datetime, timedelta

import pytest

from db_guardian.backups.models import Backup, BackupStatus, BackupTable, BackupTrigger, FileState
from db_guardian.sources.models import Source


@pytest.fixture
def seed(container):
    """Two sources; three backups for 'intranet', one for 'etl'."""
    base = datetime(2026, 11, 11, 20, 40)
    with container.session_factory() as s:
        intranet = Source(
            name="intranet", host="h", port=3306, user="u", password_enc="x",
            database="intranet", tables=["*"], schedule="0 3 * * *", retention=3,
        )
        etl = Source(
            name="etl", host="h", port=3306, user="u", password_enc="x",
            database="etl", tables=["hist_a"], schedule="0 3 * * *", retention=3,
        )
        s.add_all([intranet, etl])
        s.flush()
        rows = []
        for i, status in enumerate([BackupStatus.SUCCESS, BackupStatus.FAILED, BackupStatus.SUCCESS]):
            rows.append(Backup(
                source_id=intranet.id, source_name="intranet", trigger=BackupTrigger.SCHEDULED,
                status=status, created_at=base + timedelta(days=i),
                file_state=FileState.PRESENT if status == BackupStatus.SUCCESS else FileState.NONE,
            ))
        rows.append(Backup(
            source_id=etl.id, source_name="etl", trigger=BackupTrigger.MANUAL,
            status=BackupStatus.SUCCESS, created_at=base + timedelta(days=5), file_state=FileState.PRESENT,
        ))
        s.add_all(rows)
        s.flush()
        s.add_all([
            BackupTable(backup_id=rows[0].id, name="users", row_count=5, create_sql="CREATE TABLE `users` (...)"),
            BackupTable(backup_id=rows[0].id, name="orders", row_count=10, create_sql="CREATE TABLE `orders` (...)"),
        ])
        s.commit()
        return {"intranet": intranet.id, "etl": etl.id, "backups": [b.id for b in rows]}


def test_list_newest_first_and_total(client, seed):
    body = client.get("/backups").json()
    assert body["total"] == 4
    assert [b["id"] for b in body["items"]] == sorted(seed["backups"], reverse=True)


def test_list_pagination(client, seed):
    body = client.get("/backups", params={"limit": 2, "offset": 1}).json()
    assert body["total"] == 4
    assert [b["id"] for b in body["items"]] == sorted(seed["backups"], reverse=True)[1:3]


def test_limit_capped_at_200(client, seed):
    assert client.get("/backups", params={"limit": 201}).status_code == 422


def test_filter_by_source_and_status(client, seed):
    body = client.get("/backups", params={"source_id": seed["intranet"], "status": "success"}).json()
    assert body["total"] == 2
    assert {b["status"] for b in body["items"]} == {"success"}
    assert {b["source_name"] for b in body["items"]} == {"intranet"}


def test_filter_by_date_range(client, seed):
    # created_at values are 2026-11-11..13 and 2026-11-16 (20:40 UTC)
    body = client.get("/backups", params={"date_from": "2026-11-12T00:00:00", "date_to": "2026-11-14T00:00:00"}).json()
    assert body["total"] == 2


def test_timestamps_in_display_timezone(client, seed):
    item = client.get(f"/backups/{seed['backups'][0]}").json()
    assert item["created_at"] == "2026-11-11T15:40:00-05:00"


def test_detail_has_tables_and_totals(client, seed):
    body = client.get(f"/backups/{seed['backups'][0]}").json()
    assert body["table_count"] == 2
    assert body["total_rows"] == 15
    assert {(t["name"], t["row_count"]) for t in body["tables"]} == {("users", 5), ("orders", 10)}
    assert "create_sql" not in json.dumps(body)
    assert "file_path" not in body


def test_history_survives_source_soft_delete(client, seed):
    assert client.delete(f"/sources/{seed['intranet']}").status_code == 204
    body = client.get("/backups", params={"source_id": seed["intranet"]}).json()
    assert body["total"] == 3
    assert body["items"][0]["source_name"] == "intranet"


def test_unknown_backup_404(client):
    assert client.get("/backups/999").status_code == 404
