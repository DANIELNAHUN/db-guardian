"""Refuse clone targets that point at a configured backup source (production)."""

import ipaddress
import socket
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.sources.models import Source

LOCAL_NAMES = {"localhost", "host.docker.internal"}
_LOCAL = "local"


def _identity(address: str) -> str:
    """'local' for loopback/unspecified addresses (incl. IPv4-mapped IPv6), else the address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return address
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return _LOCAL if ip.is_loopback or ip.is_unspecified else str(ip)


def _normalize(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _host_identities(host: str, local_hosts: frozenset[str] = frozenset()) -> set[str]:
    """Names/addresses a host is known by: itself, 'local' for loopback forms, resolved IPs.

    `local_hosts` are extra names/addresses of this machine (e.g. its LAN IP), which
    reach the same MySQL as host.docker.internal from inside the container.
    """
    name = _normalize(host)
    if name in LOCAL_NAMES or name in local_hosts:
        return {_LOCAL}
    identities = {_identity(name)}
    try:
        for *_rest, sockaddr in socket.getaddrinfo(name, None):
            identities.add(_identity(sockaddr[0]))
    except (socket.gaierror, UnicodeError, OSError):
        pass
    if identities & local_hosts:
        identities.add(_LOCAL)
    return identities


def _canonical_local_hosts(hosts: Iterable[str]) -> frozenset[str]:
    """Configured names/addresses in the same forms _host_identities produces."""
    canonical = set()
    for host in hosts:
        name = _normalize(host)
        if name:
            canonical |= {name, _identity(name)}
    canonical.discard(_LOCAL)
    return frozenset(canonical)


def matches_source(
    session: Session, target: ConnectionInfo, local_hosts: Iterable[str] = ()
) -> Source | None:
    """Return the source (deleted ones included: still production) the target points at.

    Database names compare case-insensitively (servers with lower_case_table_names=1).
    """
    candidates = session.scalars(
        select(Source).where(
            Source.port == target.port,
            func.lower(Source.database) == target.database.lower(),
        )
    ).all()
    if not candidates:
        return None
    local = _canonical_local_hosts(local_hosts)
    target_ids = _host_identities(target.host, local)
    for source in candidates:
        if target_ids & _host_identities(source.host, local):
            return source
    return None
