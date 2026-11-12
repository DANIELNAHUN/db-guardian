from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConnectionInfo:
    """Connection data for a MySQL server. The password never shows in repr."""

    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str
