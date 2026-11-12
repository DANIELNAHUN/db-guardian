from datetime import datetime
from pathlib import Path

import pytest

from db_guardian.backups.models import Backup, BackupStatus, BackupTrigger, FileState
from db_guardian.downloads import api as downloads_api
from db_guardian.sources.models import Source

FILE_BYTES = b"\x1f\x8b fake gzip payload"


@pytest.fixture
def make_backup(container):
    def _make(status=BackupStatus.SUCCESS, file_state=FileState.PRESENT, with_file=True) -> Backup:
        path = container.settings.backups_dir / "1" / "20261111T204000Z_1.sql.gz"
        if with_file:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(FILE_BYTES)
        with container.session_factory() as s:
            source = Source(name="intranet", host="h", port=3306, user="u", password_enc="x",
                            database="d", tables=["*"], schedule="0 3 * * *", retention=3)
            s.add(source)
            s.flush()
            backup = Backup(
                source_id=source.id, source_name="intranet", trigger=BackupTrigger.MANUAL, status=status,
                started_at=datetime(2026, 11, 11, 20, 40), file_path=str(path), file_state=file_state,
            )
            s.add(backup)
            s.commit()
            return backup
    return _make


def signed_url(client, backup_id) -> str:
    r = client.post(f"/backups/{backup_id}/download-url")
    assert r.status_code == 200
    return r.json()["url"]


def test_roundtrip_download(client, make_backup):
    b = make_backup()
    r = client.post(f"/backups/{b.id}/download-url")
    body = r.json()
    assert body["url"].startswith(f"/downloads/{b.id}?exp=")
    assert body["expires_at"].endswith("-05:00")  # display timezone

    download = client.get(body["url"])
    assert download.status_code == 200
    assert download.content == FILE_BYTES
    assert download.headers["content-type"] == "application/gzip"
    # started 2026-11-11 20:40 UTC -> 15:40 in America/Lima
    assert download.headers["content-disposition"] == 'attachment; filename="intranet_20261111_154000.sql.gz"'


def test_url_expires_after_ttl(client, make_backup, monkeypatch, container):
    b = make_backup()
    monkeypatch.setattr(downloads_api, "_now", lambda: 1_000_000)
    url = signed_url(client, b.id)
    assert f"exp={1_000_000 + container.settings.download_ttl_seconds}" in url
    monkeypatch.setattr(downloads_api, "_now", lambda: 1_000_000 + container.settings.download_ttl_seconds + 1)
    assert client.get(url).status_code == 403


def test_tampered_sig_403(client, make_backup):
    b = make_backup()
    url = signed_url(client, b.id)
    assert client.get(url[:-1] + ("0" if url[-1] != "0" else "1")).status_code == 403


def test_valid_sig_for_other_backup_id_403(client, make_backup):
    b = make_backup()
    url = signed_url(client, b.id)
    assert client.get(url.replace(f"/downloads/{b.id}?", f"/downloads/{b.id + 1}?")).status_code == 403


def test_missing_params_rejected(client, make_backup):
    b = make_backup()
    assert client.get(f"/downloads/{b.id}").status_code in (403, 422)


def test_retention_deleted_after_signing_410(client, make_backup, container):
    b = make_backup()
    url = signed_url(client, b.id)
    with container.session_factory() as s:
        s.get(Backup, b.id).file_state = FileState.DELETED_BY_RETENTION
        s.commit()
    Path(b.file_path).unlink()
    assert client.get(url).status_code == 410


def test_file_missing_on_disk_410(client, make_backup):
    b = make_backup()
    url = signed_url(client, b.id)
    Path(b.file_path).unlink()
    assert client.get(url).status_code == 410


def test_cannot_sign_failed_backup_410(client, make_backup):
    b = make_backup(status=BackupStatus.FAILED, file_state=FileState.NONE, with_file=False)
    assert client.post(f"/backups/{b.id}/download-url").status_code == 410


def test_cannot_sign_unknown_backup_404(client):
    assert client.post("/backups/999/download-url").status_code == 404
