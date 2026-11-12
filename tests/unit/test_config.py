import pytest
from cryptography.fernet import Fernet

from db_guardian.config import ConfigError, load_settings


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name in ("ENCRYPTION_KEY", "SIGNING_KEY", "DISPLAY_TZ", "CORS_ORIGINS"):
        monkeypatch.delenv(f"GUARDIAN_{name}", raising=False)
    monkeypatch.setenv("GUARDIAN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GUARDIAN_SIGNING_KEY", "x" * 32)
    monkeypatch.setenv("GUARDIAN_DATA_DIR", str(tmp_path))
    return monkeypatch


def test_missing_encryption_key_fails(env):
    env.delenv("GUARDIAN_ENCRYPTION_KEY")
    with pytest.raises(ConfigError, match="GUARDIAN_ENCRYPTION_KEY"):
        load_settings()


def test_missing_signing_key_fails(env):
    env.delenv("GUARDIAN_SIGNING_KEY")
    with pytest.raises(ConfigError, match="GUARDIAN_SIGNING_KEY"):
        load_settings()


def test_invalid_fernet_key_fails(env):
    env.setenv("GUARDIAN_ENCRYPTION_KEY", "not-a-key")
    with pytest.raises(ConfigError, match="GUARDIAN_ENCRYPTION_KEY"):
        load_settings()


def test_short_signing_key_fails(env):
    env.setenv("GUARDIAN_SIGNING_KEY", "x" * 10)
    with pytest.raises(ConfigError, match="GUARDIAN_SIGNING_KEY"):
        load_settings()


def test_invalid_display_tz_fails(env):
    env.setenv("GUARDIAN_DISPLAY_TZ", "Mars/Base")
    with pytest.raises(ConfigError, match="GUARDIAN_DISPLAY_TZ"):
        load_settings()


def test_defaults(env, tmp_path):
    s = load_settings()
    assert s.download_ttl_seconds == 900
    assert s.default_retention == 3
    assert s.misfire_grace_seconds == 3600
    assert s.cors_origins == []
    assert s.display_tz == "UTC"
    assert s.db_path == tmp_path / "guardian.db"
    assert s.backups_dir == tmp_path / "backups"


def test_cors_origins_comma_separated(env):
    env.setenv("GUARDIAN_CORS_ORIGINS", "http://a.local, http://b.local")
    assert load_settings().cors_origins == ["http://a.local", "http://b.local"]
