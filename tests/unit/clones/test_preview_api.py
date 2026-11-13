import json
from pathlib import Path

from db_guardian.backups.models import Backup, BackupStatus
from db_guardian.mysqlclient.metadata import UnknownDatabaseError

KEY = {"X-Clone-Key": "test-clone-key"}
CUSTOMERS = "CREATE TABLE `customers` (\n  `id` int NOT NULL,\n  `name` varchar(100)\n) ENGINE=InnoDB"
CUSTOMERS_OLD = "CREATE TABLE `customers` (\n  `id` int NOT NULL,\n  `full_name` varchar(50)\n) ENGINE=InnoDB"
HIST = "CREATE TABLE `hist_sales` (\n  `id` int NOT NULL\n) ENGINE=InnoDB"


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


def test_validation_error_does_not_echo_password(client, backup, reader):
    body = {"host": "staging.example", "user": "stager", "password": "pw-123"}  # database missing
    for path in (f"/backups/{backup.id}/clone-preview", f"/backups/{backup.id}/clones"):
        r = client.post(path, json=body, headers=KEY)
        assert r.status_code == 422
        assert "database" in r.text
        assert "pw-123" not in r.text


def test_preview_warns_when_binlog_blocks_routines(client, backup, reader):
    reader.schema.binlog_blocks_routines = True
    warnings = preview(client, backup.id).json()["warnings"]
    assert any("log_bin_trust_function_creators" in w and "1419" in w for w in warnings)
