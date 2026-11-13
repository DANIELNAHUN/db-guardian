"""Refuse clone targets that point at a configured backup source (production)."""

import ipaddress
import socket

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


def _host_identities(host: str) -> set[str]:
    """Names/addresses a host is known by: itself, 'local' for loopback forms, resolved IPs."""
    name = host.strip().lower().rstrip(".")
    if name in LOCAL_NAMES:
        return {_LOCAL}
    identities = {_identity(name)}
    try:
        for *_rest, sockaddr in socket.getaddrinfo(name, None):
            identities.add(_identity(sockaddr[0]))
    except (socket.gaierror, UnicodeError, OSError):
        pass
    return identities


def matches_source(session: Session, target: ConnectionInfo) -> Source | None:
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
    target_ids = _host_identities(target.host)
    for source in candidates:
        if target_ids & _host_identities(source.host):
            return source
    return None
