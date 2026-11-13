import socket

import pytest

from db_guardian.clones import guard as guard_module
from db_guardian.clones.guard import matches_source
from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.sources.models import Source
from db_guardian.timefmt import utcnow


@pytest.fixture
def session(container):
    with container.session_factory() as s:
        def add(name, host, database, deleted=False, enabled=True):
            s.add(Source(name=name, host=host, port=3306, user="u", password_enc="x", database=database,
                         tables=["*"], schedule="0 3 * * *", retention=3, enabled=enabled,
                         deleted_at=utcnow() if deleted else None))
        add("intranet", "localhost", "intranet")
        add("etl", "192.168.1.30", "etl", enabled=False)
        add("old", "10.0.0.9", "old", deleted=True)
        s.commit()
        yield s


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        table = {"etl.lan": "192.168.1.30", "192.168.1.30": "192.168.1.30", "10.0.0.9": "10.0.0.9"}
        if host not in table:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 0))]

    monkeypatch.setattr(guard_module.socket, "getaddrinfo", fake_getaddrinfo)


def target(host, port=3306, database="intranet"):
    return ConnectionInfo(host=host, port=port, user="stager", password="pw", database=database)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "LOCALHOST", "host.docker.internal", "::1"])
def test_local_aliases_match(session, host):
    assert matches_source(session, target(host)).name == "intranet"


def test_same_ip_matches_disabled_source_too(session):
    assert matches_source(session, target("192.168.1.30", database="etl")).name == "etl"


def test_dns_resolution_matches(session):
    assert matches_source(session, target("etl.lan", database="etl")).name == "etl"


def test_different_database_no_match(session):
    assert matches_source(session, target("localhost", database="staging")) is None


def test_different_port_no_match(session):
    assert matches_source(session, target("localhost", port=3307)) is None


def test_deleted_source_still_protected(session):
    assert matches_source(session, target("10.0.0.9", database="old")).name == "old"


def test_dns_failure_is_no_match(session):
    assert matches_source(session, target("staging.example.com", database="etl")) is None


@pytest.mark.parametrize("host", ["127.0.0.2", "0.0.0.0", "::ffff:127.0.0.1", "localhost."])
def test_more_loopback_forms_match(session, host):
    assert matches_source(session, target(host)).name == "intranet"


def test_database_name_is_case_insensitive(session):
    assert matches_source(session, target("localhost", database="Intranet")).name == "intranet"
