from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db_guardian.db import Base
from db_guardian.timefmt import utcnow


class BackupStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class FileState(StrEnum):
    NONE = "none"
    PRESENT = "present"
    DELETED_BY_RETENTION = "deleted_by_retention"


class BackupTrigger(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    source_name: Mapped[str] = mapped_column(String(100))
    trigger: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    server_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_state: Mapped[str] = mapped_column(String(30), default=FileState.NONE)

    tables: Mapped[list["BackupTable"]] = relationship(
        back_populates="backup", order_by="BackupTable.id", cascade="all, delete-orphan"
    )


class BackupTable(Base):
    __tablename__ = "backup_tables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    backup_id: Mapped[int] = mapped_column(ForeignKey("backups.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    row_count: Mapped[int] = mapped_column(BigInteger)
    create_sql: Mapped[str] = mapped_column(Text)

    backup: Mapped[Backup] = relationship(back_populates="tables")
