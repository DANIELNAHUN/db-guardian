import hmac

from fastapi import Depends, Header, HTTPException, status

from db_guardian.container import Container
from db_guardian.deps import get_container


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "missing or invalid API token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_api_token(
    authorization: str | None = Header(default=None),
    c: Container = Depends(get_container),
) -> None:
    """Gate the API behind GUARDIAN_API_TOKEN (sent as "Authorization: Bearer <token>")."""
    if not authorization:
        raise _unauthorized()
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized()
    if not hmac.compare_digest(token.encode(), c.settings.api_token.encode()):
        raise _unauthorized()
