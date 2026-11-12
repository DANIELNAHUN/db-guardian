from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from db_guardian.backups.runner import BackupRunner
from db_guardian.config import Settings
from db_guardian.crypto import SecretBox
from db_guardian.db import init_db, make_engine, make_session_factory
from db_guardian.mysqlclient.dump import Dumper, dump_to_gzip
from db_guardian.mysqlclient.metadata import MetadataReader, capture_metadata


@dataclass
class Container:
    """Wires the service's long-lived collaborators."""

    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    box: SecretBox
    dumper: Dumper
    metadata_reader: MetadataReader
    runner: BackupRunner
    # Called after a source is created/updated (source) or deleted (None).
    on_source_changed: Callable[[Any, int], None] = field(default=lambda _source, _source_id: None)


def build_container(
    settings: Settings,
    *,
    dumper: Dumper = dump_to_gzip,
    metadata_reader: MetadataReader = capture_metadata,
) -> Container:
    engine = make_engine(settings.db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    box = SecretBox(settings.encryption_key)
    runner = BackupRunner(session_factory, box, settings.backups_dir, dumper, metadata_reader)
    return Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        box=box,
        dumper=dumper,
        metadata_reader=metadata_reader,
        runner=runner,
    )
