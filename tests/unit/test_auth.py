import pytest

TOKEN_DETAIL = "missing or invalid API token"
CLONE_KEY = {"X-Clone-Key": "test-clone-key"}
TARGET = {"host": "staging.example", "port": 3306, "user": "stager", "password": "pw-123", "database": "intranet"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/sources"),
        ("get", "/backups"),
        ("get", "/clones"),
        ("post", "/backups/1/download-url"),
    ],
)
def test_protected_routes_require_token(anon_client, method, path):
    response = getattr(anon_client, method)(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == TOKEN_DETAIL


def test_wrong_token_rejected(anon_client):
    response = anon_client.get("/sources", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_other_scheme_rejected(anon_client):
    response = anon_client.get("/sources", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401


def test_empty_bearer_rejected(anon_client):
    response = anon_client.get("/sources", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


def test_bearer_scheme_is_case_insensitive(anon_client, settings):
    response = anon_client.get("/sources", headers={"Authorization": f"bearer {settings.api_token}"})
    assert response.status_code == 200


def test_health_open(anon_client):
    assert anon_client.get("/health").status_code == 200


def test_download_route_open(anon_client):
    response = anon_client.get("/downloads/1?exp=1&sig=x")
    assert response.status_code == 403


def test_valid_token_passes(client):
    assert client.get("/sources").status_code == 200


def test_clone_key_still_required(client):
    response = client.post("/backups/1/clone-preview", json=TARGET)
    assert response.status_code == 401
    assert response.json()["detail"] == "missing X-Clone-Key header"


def test_token_checked_before_clone_key(anon_client):
    response = anon_client.post("/backups/1/clone-preview", json=TARGET, headers=CLONE_KEY)
    assert response.status_code == 401
    assert response.json()["detail"] == TOKEN_DETAIL


def test_wrong_token_with_right_clone_key_rejected(anon_client):
    headers = {"Authorization": "Bearer wrong", **CLONE_KEY}
    response = anon_client.post("/backups/1/clone-preview", json=TARGET, headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == TOKEN_DETAIL
