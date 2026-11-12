from datetime import datetime

import pytest
from sqlalchemy import select

from db_guardian.backups.models import Backup, BackupStatus, BackupTrigger
from db_guardian.scheduling.scheduler import missed_fire_time

LIMA = "America/Lima"  # UTC-5, no DST: 03:00 Lima == 08:00 UTC


def utc(*args) -> datetime:
    return datetime(*args)


def test_missed_within_grace():
    assert missed_fire_time("0 3 * * *", LIMA, None, utc(2026, 11, 12, 8, 30), 3600) == utc(2026, 11, 12, 8, 0)


def test_missed_at_exact_fire_time():
    assert missed_fire_time("0 3 * * *", LIMA, None, utc(2026, 11, 12, 8, 0), 3600) == utc(2026, 11, 12, 8, 0)


def test_not_missed_when_backup_started_after_fire():
    last = utc(2026, 11, 12, 8, 0, 1)
    assert missed_fire_time("0 3 * * *", LIMA, last, utc(2026, 11, 12, 8, 30), 3600) is None


def test_missed_when_last_backup_is_older():
    last = utc(2026, 11, 11, 8, 0, 1)
    assert missed_fire_time("0 3 * * *", LIMA, last, utc(2026, 11, 12, 8, 30), 3600) == utc(2026, 11, 12, 8, 0)


def test_not_missed_outside_grace():
    assert missed_fire_time("0 3 * * *", LIMA, None, utc(2026, 11, 12, 10, 30), 3600) is None


def test_latest_of_several_fire_times():
    # every 10 minutes; 08:25 with 1h grace -> latest is 08:20
    assert missed_fire_time("*/10 * * * *", LIMA, None, utc(2026, 11, 12, 8, 25), 3600) == utc(2026, 11, 12, 8, 20)


def payload(**overrides):
    data = {
        "name": "intranet", "host": "10.0.0.5", "user": "backup", "password": "secret",
        "database": "intranet", "tables": ["*"], "schedule": "0 3 * * *",
    }
    data.update(overrides)
    return data


@pytest.fixture
def scheduler(container):
    container.scheduler.start(paused=True)
    yield container.scheduler
    container.scheduler.shutdown()


def job_trigger(scheduler, source_id) -> str | None:
    job = scheduler.get_job(source_id)
    return str(job.trigger) if job else None


def test_api_changes_resync_jobs_without_restart(client, scheduler):
    created = client.post("/sources", json=payload()).json()
    sid = created["id"]
    assert "hour='3'" in job_trigger(scheduler, sid)

    client.patch(f"/sources/{sid}", json={"schedule": "30 2 * * 1-5"})
    trigger = job_trigger(scheduler, sid)
    assert "hour='2'" in trigger and "minute='30'" in trigger and "day_of_week='1-5'" in trigger

    client.patch(f"/sources/{sid}", json={"enabled": False})
    assert job_trigger(scheduler, sid) is None

    client.patch(f"/sources/{sid}", json={"enabled": True})
    assert job_trigger(scheduler, sid) is not None

    client.delete(f"/sources/{sid}")
    assert job_trigger(scheduler, sid) is None


def test_cron_runs_in_display_timezone(client, scheduler):
    sid = client.post("/sources", json=payload()).json()["id"]
    assert "America/Lima" in str(scheduler.get_job(sid).trigger.timezone)


def test_start_loads_enabled_sources(client, container):
    a = client.post("/sources", json=payload(name="a")).json()["id"]
    b = client.post("/sources", json=payload(name="b", enabled=False)).json()["id"]
    container.scheduler.start(paused=True)
    try:
        assert container.scheduler.get_job(a) is not None
        assert container.scheduler.get_job(b) is None
    finally:
        container.scheduler.shutdown()


def test_fired_job_enqueues_scheduled_backup(client, container):
    sid = client.post("/sources", json=payload()).json()["id"]
    container.scheduler.fire(sid)
    with container.session_factory() as s:
        backup = s.scalars(select(Backup)).one()
    assert backup.trigger == BackupTrigger.SCHEDULED
    assert backup.status == BackupStatus.QUEUED


def test_fired_job_for_disabled_source_is_ignored(client, container):
    sid = client.post("/sources", json=payload(enabled=False)).json()["id"]
    container.scheduler.fire(sid)  # must not raise
    with container.session_factory() as s:
        assert s.scalars(select(Backup)).all() == []


def test_catch_up_enqueues_once_per_source(client, container):
    a = client.post("/sources", json=payload(name="a")).json()["id"]
    client.post("/sources", json=payload(name="b", schedule="0 12 * * *"))
    now = utc(2026, 11, 12, 8, 30)  # 03:30 Lima: only "a" (03:00) was missed
    assert container.scheduler.catch_up(now) == [a]
    assert container.scheduler.catch_up(now) == []  # a queued backup now exists after the fire time
    with container.session_factory() as s:
        assert len(s.scalars(select(Backup)).all()) == 1


def test_catch_up_reruns_backups_interrupted_by_restart(client, container):
    from db_guardian.backups.recovery import recover_interrupted

    sid = client.post("/sources", json=payload()).json()["id"]
    now = utc(2026, 11, 12, 8, 30)  # 03:30 Lima, 03:00 run was queued then the service restarted
    assert container.scheduler.catch_up(now) == [sid]
    recover_interrupted(container.session_factory, container.settings.backups_dir)
    assert container.scheduler.catch_up(now) == [sid]


def test_catch_up_does_not_rerun_genuine_failures(client, container):
    sid = client.post("/sources", json=payload()).json()["id"]
    now = utc(2026, 11, 12, 8, 30)
    container.scheduler.catch_up(now)
    with container.session_factory() as s:
        b = s.scalars(select(Backup)).one()
        b.status = BackupStatus.FAILED
        b.error = "mysqldump exited with code 2: Access denied"
        s.commit()
    assert container.scheduler.catch_up(now) == []
