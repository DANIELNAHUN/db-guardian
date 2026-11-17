import socket
import struct
import threading

import pymysql
import pytest

from db_guardian.mysqlclient import metadata
from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.mysqlclient.metadata import TlsUnavailableError, target_schema

CONN = ConnectionInfo(host="staging.example", port=3306, user="stager", password="pw", database="staging")

CLIENT_PROTOCOL_41 = 0x0200
CLIENT_SSL = 0x0800
CLIENT_SECURE_CONNECTION = 0x8000
CLIENT_PLUGIN_AUTH = 0x80000


def handshake_packet(capabilities: int) -> bytes:
    """A MySQL protocol-10 initial handshake advertising `capabilities`."""
    payload = (
        b"\x0a" + b"8.0.46\x00" + struct.pack("<I", 7) + b"abcdefgh\x00"
        + struct.pack("<H", capabilities & 0xFFFF) + b"\x21" + struct.pack("<H", 2)
        + struct.pack("<H", capabilities >> 16) + b"\x15" + b"\x00" * 10
        + b"ijklmnopqrst\x00" + b"mysql_native_password\x00"
    )
    return struct.pack("<I", len(payload))[:3] + b"\x00" + payload


class FakeServer:
    """Sends one handshake and records every byte the client answers with."""

    def __init__(self, capabilities: int):
        self.capabilities = capabilities
        self.received = b""
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        client, _ = self.sock.accept()
        with client:
            client.settimeout(2)
            client.sendall(handshake_packet(self.capabilities))
            try:
                while chunk := client.recv(4096):
                    self.received += chunk
            except OSError:
                pass

    def close(self):
        self.thread.join(timeout=5)
        self.sock.close()


@pytest.fixture
def plaintext_server():
    server = FakeServer(CLIENT_PROTOCOL_41 | CLIENT_SECURE_CONNECTION | CLIENT_PLUGIN_AUTH)
    yield server
    server.close()


def test_server_without_tls_is_refused_before_credentials_are_sent(plaintext_server):
    conn = ConnectionInfo(host="127.0.0.1", port=plaintext_server.port, user="stager", password="pw-123",
                          database="staging")
    with pytest.raises(TlsUnavailableError, match="does not offer TLS"):
        metadata._connect_tls(conn)
    plaintext_server.close()
    assert plaintext_server.received == b""


def test_tls_refusal_keeps_its_connection_error_contract():
    assert issubclass(TlsUnavailableError, ConnectionError)


def test_server_with_tls_proceeds_to_authentication(monkeypatch):
    sent = []
    monkeypatch.setattr(pymysql.connections.Connection, "_request_authentication", lambda self: sent.append("auth"))
    connection = object.__new__(metadata._TlsOnlyConnection)
    connection.server_capabilities = CLIENT_SSL
    connection._request_authentication()
    assert sent == ["auth"]


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
