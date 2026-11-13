from dataclasses import replace

import pytest

from db_guardian.mysqlclient.dump import dump_to_gzip
from db_guardian.mysqlclient.metadata import server_version
from db_guardian.mysqlclient.restore import RestoreError, restore_gzip

pytestmark = pytest.mark.integration


def test_full_restore_as_unprivileged_user(conn80, staging, sql80, tmp_path):
    backup = tmp_path / "b.sql.gz"
    dump_to_gzip(conn80, None, server_version(conn80), backup)
    progress = []

    restore_gzip(staging, backup, on_progress=progress.append)

    db = staging.database
    counts = {t: sql80(f"SELECT COUNT(*) FROM `{t}`", db)[0][0] for t in ("customers", "orders", "hist_sales")}
    assert counts == {"customers": 3, "orders": 5, "hist_sales": 7}
    assert "full_name" not in sql80("SHOW CREATE TABLE customers", db)[0][1]
    assert sql80("SELECT COUNT(*) FROM extra_only", db)[0][0] == 1
    assert sql80("SELECT COUNT(*) FROM v_orders", db)[0][0] == 5
    assert sql80("SHOW TRIGGERS", db)[0][0] == "trg_orders_total"
    assert [p[1] for p in sql80(f"SHOW PROCEDURE STATUS WHERE Db = '{db}'")] == ["count_orders"]
    assert progress[-1] == 1.0


def test_restore_bad_credentials_raises(conn80, staging, tmp_path):
    backup = tmp_path / "b.sql.gz"
    dump_to_gzip(conn80, ["hist_sales"], server_version(conn80), backup)
    with pytest.raises(RestoreError) as exc_info:
        restore_gzip(replace(staging, password="wrong"), backup)
    assert "Access denied" in exc_info.value.stderr_tail


def test_binlog_without_trust_reports_1419(conn80, staging, sql80, tmp_path):
    """Without log_bin_trust_function_creators a non-SUPER target user cannot create triggers."""
    backup = tmp_path / "b.sql.gz"
    dump_to_gzip(conn80, None, server_version(conn80), backup)
    sql80("SET GLOBAL log_bin_trust_function_creators = 0")
    try:
        with pytest.raises(RestoreError) as exc_info:
            restore_gzip(staging, backup)
    finally:
        sql80("SET GLOBAL log_bin_trust_function_creators = 1")
    assert "1419" in exc_info.value.stderr_tail
    assert "log_bin_trust_function_creators" in exc_info.value.stderr_tail
