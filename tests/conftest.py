import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from db_guardian.app import create_app
from db_guardian.config import Settings
from db_guardian.container import build_container


@pytest.fixture
def settings(tmp_path):
    return Settings(
        encryption_key=Fernet.generate_key().decode(),
        signing_key="s" * 32,
        data_dir=tmp_path / "data",
        display_tz="America/Lima",
        clone_key="test-clone-key",
    )


@pytest.fixture
def container(settings):
    return build_container(settings)


@pytest.fixture
def client(container):
    with TestClient(create_app(container, start_background=False)) as c:
        yield c
