from db_guardian.backups.models import Backup, BackupStatus, BackupTrigger
from db_guardian.backups.recovery import recover_interrupted
from db_guardian.sources.models import Source


def test_recovery_marks_interrupted_and_removes_tmp(container):
    backups_dir = container.settings.backups_dir
    (backups_dir / "1").mkdir(parents=True)
    stray = backups_dir / "1" / "20261112T030000Z_2.sql.tmp"
    stray.write_bytes(b"partial")
    kept = backups_dir / "1" / "20261111T030000Z_1.sql.gz"
    kept.write_bytes(b"good")

    with container.session_factory() as s:
        source = Source(name="intranet", host="h", port=3306, user="u", password_enc="x",
                        database="d", tables=["*"], schedule="0 3 * * *", retention=3)
        s.add(source)
        s.flush()
        statuses = [BackupStatus.SUCCESS, BackupStatus.RUNNING, BackupStatus.QUEUED, BackupStatus.FAILED]
        rows = [Backup(source_id=source.id, source_name="intranet", trigger=BackupTrigger.SCHEDULED, status=st)
                for st in statuses]
        s.add_all(rows)
        s.commit()
        ids = [r.id for r in rows]

    assert recover_interrupted(container.session_factory, backups_dir) == 2

    with container.session_factory() as s:
        result = [s.get(Backup, i) for i in ids]
        assert [b.status for b in result] == ["success", "failed", "failed", "failed"]
        for b in result[1:3]:
            assert b.error == "interrupted: service stopped before completion"
            assert b.finished_at is not None
        assert result[3].error is None
    assert not stray.exists()
    assert kept.exists()


def test_recovery_with_no_backups_dir(container):
    assert recover_interrupted(container.session_factory, container.settings.backups_dir) == 0


def test_sweep_removes_leftover_dump_workdirs(tmp_path):
    from db_guardian.backups.recovery import sweep_dump_workdirs

    leftover = tmp_path / "guardian-dump-abc123"
    leftover.mkdir()
    (leftover / "client.cnf").write_text('[client]\npassword="secret"\n')
    unrelated = tmp_path / "other-dir"
    unrelated.mkdir()
    assert sweep_dump_workdirs(tmp_path) == 1
    assert not leftover.exists()
    assert unrelated.exists()
