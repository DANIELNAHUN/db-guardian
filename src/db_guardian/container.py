from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from db_guardian.config import Settings
from db_guardian.crypto import SecretBox
from db_guardian.db import init_db, make_engine, make_session_factory


@dataclass
class Container:
    """Wires the service's long-lived collaborators."""

    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    box: SecretBox
    # Called after a source is created/updated (source) or deleted (None).
    on_source_changed: Callable[[Any, int], None] = field(default=lambda _source, _source_id: None)


def build_container(settings: Settings, **overrides) -> Container:
    engine = make_engine(settings.db_path)
    init_db(engine)
    parts = {
        "settings": settings,
        "engine": engine,
        "session_factory": make_session_factory(engine),
        "box": SecretBox(settings.encryption_key),
    }
    parts.update(overrides)
    return Container(**parts)
