"""Refuse clone targets that point at a configured backup source (production)."""

import socket

from sqlalchemy import select
from sqlalchemy.orm import Session

from db_guardian.mysqlclient.connection import ConnectionInfo
from db_guardian.sources.models import Source

LOCAL_ALIASES = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
_LOCAL = "local"


def _host_identities(host: str) -> set[str]:
    """Names/addresses a host is known by: itself, 'local' for aliases, resolved IPs."""
    name = host.strip().lower()
    if name in LOCAL_ALIASES:
        return {_LOCAL}
    identities = {name}
    try:
        for *_rest, sockaddr in socket.getaddrinfo(name, None):
            address = sockaddr[0]
            identities.add(_LOCAL if address in LOCAL_ALIASES else address)
    except (socket.gaierror, UnicodeError, OSError):
        pass
    return identities


def matches_source(session: Session, target: ConnectionInfo) -> Source | None:
    """Return the non-deleted source that the target points at, if any."""
    candidates = session.scalars(
        select(Source).where(
            Source.deleted_at.is_(None),
            Source.port == target.port,
            Source.database == target.database,
        )
    ).all()
    if not candidates:
        return None
    target_ids = _host_identities(target.host)
    for source in candidates:
        if target_ids & _host_identities(source.host):
            return source
    return None
