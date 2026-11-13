import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from db_guardian.clones.security import redact, require_clone_key
from db_guardian.container import build_container


def make_client(settings, clone_key):
    container = build_container(settings.model_copy(update={"clone_key": clone_key}))
    app = FastAPI()
    app.state.container = container

    @app.post("/guarded", dependencies=[Depends(require_clone_key)])
    def guarded():
        return {"ok": True}

    return TestClient(app)


def test_unset_key_503(settings):
    r = make_client(settings, None).post("/guarded", headers={"X-Clone-Key": "anything"})
    assert r.status_code == 503
    assert "GUARDIAN_CLONE_KEY" in r.json()["detail"]


def test_missing_header_401(settings):
    assert make_client(settings, "right-key").post("/guarded").status_code == 401


def test_wrong_key_403(settings):
    r = make_client(settings, "right-key").post("/guarded", headers={"X-Clone-Key": "wrong"})
    assert r.status_code == 403


def test_non_ascii_key_403(settings):
    r = make_client(settings, "right-key").post("/guarded", headers={"X-Clone-Key": "ñandú".encode()})
    assert r.status_code == 403


def test_right_key_passes(settings):
    r = make_client(settings, "right-key").post("/guarded", headers={"X-Clone-Key": "right-key"})
    assert r.status_code == 200


@pytest.mark.parametrize("message, secret, expected", [
    ("Access denied pw=s3cret", "s3cret", "Access denied pw=***"),
    ("Access denied", "", "Access denied"),
])
def test_redact(message, secret, expected):
    assert redact(message, secret) == expected
