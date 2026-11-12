import hashlib
import hmac


def sign(backup_id: int, expires_at: int, key: str) -> str:
    message = f"{backup_id}:{expires_at}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def verify(backup_id: int, expires_at: int, sig: str, key: str, now: int) -> bool:
    if now > expires_at:
        return False
    return hmac.compare_digest(sign(backup_id, expires_at, key), sig)
