import json
from pathlib import Path

import pytest

from db_guardian.backups.models import Backup, BackupStatus, BackupTable, BackupTrigger, FileState
from db_guardian.mysqlclient.metadata import TargetSchema, UnknownDatabaseError
from db_guardian.sources.models import Source

KEY = {"X-Clone-Key": "test-clone-key"}
CUSTOMERS = "CREATE TABLE `customers` (\n  `id` int NOT NULL,\n  `name` varchar(100)\n) ENGINE=InnoDB"
CUSTOMERS_OLD = "CREATE TABLE `customers` (\n  `id` int NOT NULL,\n  `full_name` varchar(50)\n) ENGINE=InnoDB"
ORDERS = "CREATE TABLE `orders` (\n  `id` int NOT NULL\n) ENGINE=InnoDB AUTO_INCREMENT=6"
HIST = "CREATE TABLE `hist_sales` (\n  `id` int NOT NULL\n) ENGINE=InnoDB"
EXTRA = "CREATE TABLE `extra_only` (\n  `id` int NOT NULL\n) ENGINE=InnoDB"


class FakeReader:
    def __init__(self):
        self.calls = []
        self.error = None
        self.schema = TargetSchema(
            server_version="8.0.46",
            tables={"customers": CUSTOMERS_OLD, "orders": ORDERS.replace("=6", "=2"), "extra_only": EXTRA},
        )

    def __call__(self, conn):
        self.calls.append(conn)
        if self.error:
            raise self.error
        return self.schema


@pytest.fixture
def reader(container):
    fake = FakeReader()
    container.target_reader = fake
    return fake


@pytest.fixture
def backup(container):
    path = container.settings.backups_dir / "1" / "b.sql.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"gz")
    with container.session_factory() as s:
        source = Source(name="intranet", host="localhost", port=3306, user="u", password_enc="x",
                        database="intranet", tables=["*"], schedule="0 3 * * *", retention=3)
        s.add(source)
        s.flush()
        b = Backup(source_id=source.id, source_name="intranet", trigger=BackupTrigger.MANUAL,
                   status=BackupStatus.SUCCESS, file_state=FileState.PRESENT, file_path=str(path),
                   server_version="8.0.46")
        b.tables = [BackupTable(name=n, row_count=1, create_sql=sql)
                    for n, sql in (("customers", CUSTOMERS), ("orders", ORDERS), ("hist_sales", HIST))]
        s.add(b)
        s.commit()
        return b


def target(**overrides):
    data = {"host": "staging.example", "port": 3306, "user": "stager", "password": "pw-123", "database": "intranet"}
    data.update(overrides)
    return data


def preview(client, backup_id, headers=KEY, **overrides):
    return client.post(f"/backups/{backup_id}/clone-preview", json=target(**overrides), headers=headers)


def test_preview_requires_key(client, backup, reader):
    assert preview(client, backup.id, headers={}).status_code == 401
    assert reader.calls == []


def test_preview_report(client, backup, reader):
    r = preview(client, backup.id)
    assert r.status_code == 200
    body = r.json()
    assert body["backup_id"] == backup.id
    assert body["missing_in_target"] == [{"name": "hist_sales", "create_sql": HIST}]
    assert [d["name"] for d in body["structure_differs"]] == ["customers"]
    assert body["structure_differs"][0]["backup_create_sql"] == CUSTOMERS
    assert body["structure_differs"][0]["target_create_sql"] == CUSTOMERS_OLD
    assert body["only_in_target"] == ["extra_only"]
    assert body["matching"] == ["orders"]
    assert body["warnings"] == []
    assert body["source_version"] == "8.0.46"
    assert body["target_version"] == "8.0.46"
    assert "pw-123" not in json.dumps(body)
    assert reader.calls[0].password == "pw-123"


def test_preview_refuses_production_alias(client, backup, reader):
    r = preview(client, backup.id, host="127.0.0.1")
    assert r.status_code == 409
    assert "intranet" in r.json()["detail"]
    assert reader.calls == []


def test_preview_backup_file_gone_410(client, backup, reader):
    Path(backup.file_path).unlink()
    assert preview(client, backup.id).status_code == 410


def test_preview_unknown_backup_404(client, reader):
    assert preview(client, 999).status_code == 404


def test_preview_unknown_target_db_422(client, backup, reader):
    reader.error = UnknownDatabaseError("intranet")
    r = preview(client, backup.id)
    assert r.status_code == 422
    assert "intranet" in r.json()["detail"]


def test_preview_target_error_502_redacted(client, backup, reader):
    reader.error = OSError("(1045) Access denied for 'stager' using password 'pw-123'")
    r = preview(client, backup.id)
    assert r.status_code == 502
    assert "Access denied" in r.json()["detail"]
    assert "pw-123" not in r.text


def test_preview_writes_nothing(client, backup, reader, container):
    before = sorted(p.name for p in container.settings.backups_dir.rglob("*"))
    preview(client, backup.id)
    assert sorted(p.name for p in container.settings.backups_dir.rglob("*")) == before
    with container.session_factory() as s:
        assert s.get(Backup, backup.id).status == BackupStatus.SUCCESS
