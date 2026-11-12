import json

from sqlalchemy import select

from db_guardian.sources.models import Source


def payload(**overrides):
    data = {
        "name": "intranet",
        "host": "10.0.0.5",
        "port": 3306,
        "user": "backup",
        "password": "secret",
        "database": "intranet",
        "tables": ["*"],
        "schedule": "0 3 * * *",
    }
    data.update(overrides)
    return data


def test_create_returns_201_with_defaults(client):
    r = client.post("/sources", json=payload())
    assert r.status_code == 201
    body = r.json()
    assert body["retention"] == 3
    assert body["enabled"] is True
    assert body["has_password"] is True
    assert body["tables"] == ["*"]


def test_password_never_returned(client):
    created = client.post("/sources", json=payload()).json()
    bodies = [
        created,
        client.get(f"/sources/{created['id']}").json(),
        client.get("/sources").json()[0],
        client.patch(f"/sources/{created['id']}", json={"port": 3307}).json(),
    ]
    for body in bodies:
        assert "password" not in body
        assert "secret" not in json.dumps(body)


def test_password_stored_encrypted(client, container):
    created = client.post("/sources", json=payload()).json()
    with container.session_factory() as s:
        row = s.get(Source, created["id"])
        assert row.password_enc != "secret"
        assert container.box.decrypt(row.password_enc) == "secret"


def test_patch_without_password_keeps_it(client, container):
    created = client.post("/sources", json=payload()).json()
    r = client.patch(f"/sources/{created['id']}", json={"schedule": "30 2 * * 1-5"})
    assert r.status_code == 200
    assert r.json()["schedule"] == "30 2 * * 1-5"
    with container.session_factory() as s:
        assert container.box.decrypt(s.get(Source, created["id"]).password_enc) == "secret"


def test_patch_with_password_replaces_it(client, container):
    created = client.post("/sources", json=payload()).json()
    client.patch(f"/sources/{created['id']}", json={"password": "new"})
    with container.session_factory() as s:
        assert container.box.decrypt(s.get(Source, created["id"]).password_enc) == "new"


def test_patch_invalid_tables_422(client):
    created = client.post("/sources", json=payload()).json()
    assert client.patch(f"/sources/{created['id']}", json={"tables": []}).status_code == 422


def test_duplicate_name_409(client):
    client.post("/sources", json=payload())
    assert client.post("/sources", json=payload()).status_code == 409


def test_rename_to_existing_name_409(client):
    client.post("/sources", json=payload())
    other = client.post("/sources", json=payload(name="etl")).json()
    assert client.patch(f"/sources/{other['id']}", json={"name": "intranet"}).status_code == 409


def test_delete_is_soft_and_hides_source(client, container):
    created = client.post("/sources", json=payload()).json()
    assert client.delete(f"/sources/{created['id']}").status_code == 204
    assert client.get(f"/sources/{created['id']}").status_code == 404
    assert client.get("/sources").json() == []
    with container.session_factory() as s:
        assert s.scalars(select(Source)).one().deleted_at is not None


def test_deleted_name_can_be_reused(client):
    created = client.post("/sources", json=payload()).json()
    client.delete(f"/sources/{created['id']}")
    assert client.post("/sources", json=payload()).status_code == 201


def test_unknown_source_404(client):
    assert client.get("/sources/999").status_code == 404
    assert client.patch("/sources/999", json={"port": 1}).status_code == 404
    assert client.delete("/sources/999").status_code == 404


def test_on_source_changed_hook_called(client, container):
    calls = []
    container.on_source_changed = lambda source, source_id: calls.append((source is not None, source_id))
    created = client.post("/sources", json=payload()).json()
    client.patch(f"/sources/{created['id']}", json={"enabled": False})
    client.delete(f"/sources/{created['id']}")
    assert calls == [(True, created["id"]), (True, created["id"]), (False, created["id"])]


def test_connection_info_masks_password(container, client):
    from db_guardian.sources.service import SourceService

    created = client.post("/sources", json=payload()).json()
    with container.session_factory() as s:
        service = SourceService(s, container.box)
        conn = service.connection(service.get(created["id"]))
    assert conn.password == "secret"
    assert "secret" not in repr(conn)
