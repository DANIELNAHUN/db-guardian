from functools import cached_property
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_PREFIX = "GUARDIAN_"


class ConfigError(RuntimeError):
    """Raised when the service configuration is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX)

    encryption_key: str
    signing_key: str = Field(min_length=32)
    clone_key: str | None = None
    download_ttl_seconds: int = Field(default=900, ge=1)
    display_tz: str = "UTC"
    cors_origins: Annotated[list[str], NoDecode] = []
    data_dir: Path = Path("/data")
    default_retention: int = Field(default=3, ge=1)
    misfire_grace_seconds: int = Field(default=3600, ge=0)

    @field_validator("encryption_key")
    @classmethod
    def _valid_fernet_key(cls, value: str) -> str:
        try:
            Fernet(value.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("must be a valid Fernet key") from exc
        return value

    @field_validator("display_tz")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @cached_property
    def db_path(self) -> Path:
        return self.data_dir / "guardian.db"

    @cached_property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"


def load_settings() -> Settings:
    """Load settings from the environment, raising ConfigError naming the bad variable."""
    try:
        return Settings()
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            field = str(error["loc"][0]) if error["loc"] else "?"
            problems.append(f"{ENV_PREFIX}{field.upper()}: {error['msg']}")
        raise ConfigError("Invalid configuration: " + "; ".join(problems)) from None
