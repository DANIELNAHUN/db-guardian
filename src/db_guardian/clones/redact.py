def redact(message: str, secret: str) -> str:
    """Hide a secret inside an error message (no-op for an empty secret)."""
    return message.replace(secret, "***") if secret else message
