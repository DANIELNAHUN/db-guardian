import pymysql
import pytest
from pymysql.constants import CLIENT

from db_guardian.mysqlclient import metadata
from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.mysqlclient.metadata import TlsUnavailableError, target_schema

CONN = ConnectionInfo(host="staging.example", port=3306, user="stager", password="pw", database="staging")


def handshake(capabilities: int, monkeypatch) -> list[str]:
    """Run the TLS-only connection's auth step against a fake server handshake."""
    sent = []
    monkeypatch.setattr(pymysql.connections.Connection, "_request_authentication", lambda self: sent.append("auth"))
    connection = object.__new__(metadata._TlsOnlyConnection)
    connection.server_capabilities = capabilities
    connection.host, connection.port = CONN.host, CONN.port
    connection._request_authentication()
    return sent


def test_server_without_tls_is_refused_before_credentials_are_sent(monkeypatch):
    sent = []
    with pytest.raises(TlsUnavailableError, match="does not offer TLS"):
        sent = handshake(0, monkeypatch)
    assert sent == []


def test_server_with_tls_proceeds_to_authentication(monkeypatch):
    assert handshake(CLIENT.SSL, monkeypatch) == ["auth"]


def test_tls_refusal_is_not_wrapped_as_connection_error():
    # PyMySQL turns any OSError raised during connect into a generic "Can't connect" error.
    assert not issubclass(TlsUnavailableError, OSError)


class FakeCursor:
    def __init__(self, trust_error: Exception | None):
        self.trust_error = trust_error
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql):
        if "log_bin_trust_function_creators" in sql:
            if self.trust_error:
                raise self.trust_error
            self.result = [(0,)]
        elif sql.startswith("SELECT VERSION()"):
            self.result = [("8.4.0", 1)]
        elif sql.startswith("SHOW FULL TABLES"):
            self.result = []

    def fetchone(self):
        return self.result[0]

    def fetchall(self):
        return self.result


class FakeDb:
    def __init__(self, trust_error=None):
        self.trust_error = trust_error

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return FakeCursor(self.trust_error)


def test_binlog_warning_when_trust_is_off(monkeypatch):
    monkeypatch.setattr(metadata, "_connect_tls", lambda conn: FakeDb())
    assert target_schema(CONN).binlog_blocks_routines is True


def test_trust_variable_read_is_best_effort(monkeypatch):
    error = pymysql.err.OperationalError(1193, "Unknown system variable 'log_bin_trust_function_creators'")
    monkeypatch.setattr(metadata, "_connect_tls", lambda conn: FakeDb(error))
    schema = target_schema(CONN)
    assert schema.server_version == "8.4.0"
    assert schema.binlog_blocks_routines is False
