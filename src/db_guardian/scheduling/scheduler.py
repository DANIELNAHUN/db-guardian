import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from apscheduler.job import Job
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from db_guardian.backups.models import Backup, BackupTrigger
from db_guardian.backups.recovery import INTERRUPTED
from db_guardian.backups.service import SourceUnavailable, enqueue_backup
from db_guardian.sources.models import Source

if TYPE_CHECKING:
    from db_guardian.container import Container

logger = logging.getLogger(__name__)


def _trigger(schedule: str, tz: str) -> CronTrigger:
    return CronTrigger.from_crontab(schedule, timezone=ZoneInfo(tz))


def missed_fire_time(
    schedule: str, tz: str, last_started_at: datetime | None, now: datetime, grace_seconds: int
) -> datetime | None:
    """Latest cron fire time in (now - grace, now] not yet covered by a backup.

    All datetimes are naive UTC; the cron expression is evaluated in `tz`.
    """
    trigger = _trigger(schedule, tz)
    now_aware = now.replace(tzinfo=UTC)
    cursor = now_aware - timedelta(seconds=grace_seconds) + timedelta(microseconds=1)
    latest = None
    fire = trigger.get_next_fire_time(None, cursor)
    while fire is not None and fire <= now_aware:
        latest = fire
        fire = trigger.get_next_fire_time(fire, fire + timedelta(seconds=1))
    if latest is None:
        return None
    latest_utc = latest.astimezone(UTC).replace(tzinfo=None)
    if last_started_at is not None and last_started_at >= latest_utc:
        return None
    return latest_utc


class GuardianScheduler:
    """Keeps one cron job per enabled source; jobs only enqueue work."""

    def __init__(self, container: "Container") -> None:
        self._container = container
        settings = container.settings
        self._tz = settings.display_tz
        self._scheduler = BackgroundScheduler(
            timezone=ZoneInfo(self._tz),
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": settings.misfire_grace_seconds,
            },
        )

    def start(self, paused: bool = False) -> None:
        with self._container.session_factory() as session:
            for source in session.scalars(select(Source)):
                self.sync_source(source, source.id)
        self._scheduler.start(paused=paused)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def sync_source(self, source: Source | None, source_id: int) -> None:
        job_id = self._job_id(source_id)
        if source is None or source.deleted_at is not None or not source.enabled:
            try:
                self._scheduler.remove_job(job_id)
            except JobLookupError:
                pass
            return
        self._scheduler.add_job(
            self.fire,
            trigger=_trigger(source.schedule, self._tz),
            args=[source_id],
            id=job_id,
            name=f"backup {source.name}",
            replace_existing=True,
        )

    def get_job(self, source_id: int) -> Job | None:
        return self._scheduler.get_job(self._job_id(source_id))

    def fire(self, source_id: int) -> None:
        """Cron callback: queue a scheduled backup; disabled/deleted sources are skipped."""
        try:
            enqueue_backup(self._container, source_id, BackupTrigger.SCHEDULED)
        except SourceUnavailable:
            logger.info("skipping scheduled backup of unavailable source %s", source_id)

    def catch_up(self, now: datetime) -> list[int]:
        """Queue one backup per source whose scheduled run was missed within the grace window."""
        grace = self._container.settings.misfire_grace_seconds
        enqueued = []
        with self._container.session_factory() as session:
            sources = session.scalars(
                select(Source).where(Source.deleted_at.is_(None), Source.enabled.is_(True))
            ).all()
            last_by_source = dict(
                session.execute(
                    # Runs interrupted by a restart do not count: they never completed.
                    select(Backup.source_id, func.max(Backup.created_at))
                    .where((Backup.error.is_(None)) | (Backup.error != INTERRUPTED))
                    .group_by(Backup.source_id)
                ).all()
            )
        for source in sources:
            missed = missed_fire_time(source.schedule, self._tz, last_by_source.get(source.id), now, grace)
            if missed is None:
                continue
            try:
                _backup, created = enqueue_backup(self._container, source.id, BackupTrigger.SCHEDULED)
            except SourceUnavailable:
                continue
            if created:
                logger.info("catching up missed backup of %s scheduled at %s UTC", source.name, missed)
                enqueued.append(source.id)
        return enqueued

    @staticmethod
    def _job_id(source_id: int) -> str:
        return f"source:{source_id}"
