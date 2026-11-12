from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from db_guardian.db import Base
from db_guardian.timefmt import utcnow

ALL_TABLES = "*"


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        # Names are unique among non-deleted sources, so a deleted name can be reused.
        Index("uq_sources_name_active", "name", unique=True, sqlite_where=text("deleted_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    user: Mapped[str] = mapped_column(String(100))
    password_enc: Mapped[str] = mapped_column(String)
    database: Mapped[str] = mapped_column(String(100))
    tables: Mapped[list[str]] = mapped_column(JSON)
    schedule: Mapped[str] = mapped_column(String(100))
    retention: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def selected_tables(tables: list[str]) -> list[str] | None:
    """Return the explicit table list, or None when the source selects all tables."""
    return None if tables == [ALL_TABLES] else list(tables)
