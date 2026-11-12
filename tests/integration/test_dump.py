import gzip
from dataclasses import replace

import pytest

from db_guardian.mysqlclient.dump import DumpError, dump_to_gzip
from db_guardian.mysqlclient.metadata import server_version

pytestmark = pytest.mark.integration


def test_dump_full_restores(conn, sql, restore_into, tmp_path):
    dest = tmp_path / "full.sql.gz"
    size = dump_to_gzip(conn, None, server_version(conn), dest)
    assert size == dest.stat().st_size > 0

    restore_into(dest, "restore_check")
    counts = {
        t: sql(f"SELECT COUNT(*) FROM `{t}`", "restore_check")[0][0]
        for t in ("customers", "orders", "hist_sales")
    }
    assert counts == {"customers": 3, "orders": 5, "hist_sales": 7}
    assert sql("SELECT COUNT(*) FROM v_orders", "restore_check")[0][0] == 5
    assert sql("SHOW TRIGGERS", "restore_check")[0][0] == "trg_orders_total"
    procs = sql("SHOW PROCEDURE STATUS WHERE Db = 'restore_check'")
    assert [p[1] for p in procs] == ["count_orders"]


def test_dump_table_list_only_contains_those(conn, tmp_path):
    dest = tmp_path / "partial.sql.gz"
    dump_to_gzip(conn, ["hist_sales"], server_version(conn), dest)
    text = gzip.decompress(dest.read_bytes()).decode()
    assert "CREATE TABLE `hist_sales`" in text
    assert "CREATE TABLE `orders`" not in text
    assert "DROP TABLE IF EXISTS `hist_sales`" in text


def test_wrong_password_raises_dump_error(conn, tmp_path):
    with pytest.raises(DumpError) as exc_info:
        dump_to_gzip(replace(conn, password="wrong"), None, server_version(conn), tmp_path / "x.sql.gz")
    assert "Access denied" in exc_info.value.stderr_tail
