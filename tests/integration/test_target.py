from dataclasses import replace

import pytest

from db_guardian.mysqlclient.metadata import UnknownDatabaseError, target_schema, target_session_cipher

pytestmark = pytest.mark.integration


def test_reads_target_tables(staging):
    schema = target_schema(staging)
    assert set(schema.tables) == {"customers", "extra_only"}
    assert "full_name" in schema.tables["customers"]
    assert schema.server_version.startswith("8.0")


def test_connection_uses_tls(staging):
    assert target_session_cipher(staging) != ""


def test_unknown_database(staging):
    with pytest.raises(UnknownDatabaseError) as exc_info:
        target_schema(replace(staging, database="nope_db"))
    assert exc_info.value.database == "nope_db"


def test_reports_binlog_blocking_routines(staging, sql80):
    assert target_schema(staging).binlog_blocks_routines is False  # test server trusts creators
    sql80("SET GLOBAL log_bin_trust_function_creators = 0")
    try:
        assert target_schema(staging).binlog_blocks_routines is True
    finally:
        sql80("SET GLOBAL log_bin_trust_function_creators = 1")
