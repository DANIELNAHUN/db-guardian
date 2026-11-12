import pytest

from db_guardian.mysqlclient.metadata import MissingTablesError, capture_metadata, server_version

pytestmark = pytest.mark.integration


def test_server_version(conn):
    assert server_version(conn)[0] in "58"


def test_capture_all_tables(conn):
    meta = capture_metadata(conn, None)
    assert {t.name: t.row_count for t in meta.tables} == {"customers": 3, "orders": 5, "hist_sales": 7}
    assert all(t.create_sql.startswith("CREATE TABLE") for t in meta.tables)  # view excluded
    assert meta.server_version == server_version(conn)


def test_capture_selected(conn):
    meta = capture_metadata(conn, ["hist_sales"])
    assert [(t.name, t.row_count) for t in meta.tables] == [("hist_sales", 7)]


def test_missing_table_raises_before_querying(conn):
    with pytest.raises(MissingTablesError) as exc_info:
        capture_metadata(conn, ["hist_sales", "nope`; DROP TABLE orders; --"])
    assert exc_info.value.missing == ["nope`; DROP TABLE orders; --"]
    assert "nope" in str(exc_info.value)
    assert {t.name for t in capture_metadata(conn, None).tables} == {"customers", "orders", "hist_sales"}
