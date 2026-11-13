import hmac

from fastapi import Depends, Header, HTTPException, status

from db_guardian.clones.redact import redact  # noqa: F401  (re-exported)
from db_guardian.container import Container
from db_guardian.deps import get_container


def require_clone_key(
    x_clone_key: str | None = Header(default=None),
    c: Container = Depends(get_container),
) -> None:
    """Gate clone operations behind GUARDIAN_CLONE_KEY (sent as the X-Clone-Key header)."""
    expected = c.settings.clone_key
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "clone disabled: GUARDIAN_CLONE_KEY is not set"
        )
    if x_clone_key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing X-Clone-Key header")
    if not hmac.compare_digest(x_clone_key.encode(), expected.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid clone key")
