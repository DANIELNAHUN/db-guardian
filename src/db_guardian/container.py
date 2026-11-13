from collections.abc import Callable
from functools import partial
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from db_guardian.backups.runner import BackupRunner
from db_guardian.config import Settings
from db_guardian.crypto import SecretBox
from db_guardian.db import init_db, make_engine, make_session_factory
from db_guardian.mysqlclient.dump import Dumper, dump_to_gzip
from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.mysqlclient.metadata import (
    MetadataReader,
    TargetReader,
    capture_metadata,
    server_version,
    target_schema,
)
from db_guardian.scheduling.queue import WorkQueue
from db_guardian.scheduling.scheduler import GuardianScheduler


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
    queue: WorkQueue
    version_reader: Callable[[ConnectionInfo], str]
    target_reader: TargetReader
    # Called after a source is created/updated (source) or deleted (None).
    on_source_changed: Callable[[Any, int], None] = field(default=lambda _source, _source_id: None)
    scheduler: GuardianScheduler | None = None


def build_container(
    settings: Settings,
    *,
    dumper: Dumper | None = None,
    metadata_reader: MetadataReader = capture_metadata,
    version_reader: Callable[[ConnectionInfo], str] = server_version,
    target_reader: TargetReader = target_schema,
) -> Container:
    engine = make_engine(settings.db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    box = SecretBox(settings.encryption_key)
    if dumper is None:
        dumper = partial(dump_to_gzip, timeout_seconds=settings.dump_timeout_seconds)
    runner = BackupRunner(session_factory, box, settings.backups_dir, dumper, metadata_reader)
    container = Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        box=box,
        dumper=dumper,
        metadata_reader=metadata_reader,
        runner=runner,
        queue=WorkQueue(),
        version_reader=version_reader,
        target_reader=target_reader,
    )
    container.scheduler = GuardianScheduler(container)
    container.on_source_changed = container.scheduler.sync_source
    return container
