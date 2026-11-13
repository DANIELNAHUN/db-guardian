"""Imports every ORM model so Base.metadata knows all tables."""

from db_guardian.sources.models import Source  # noqa: F401
from db_guardian.backups.models import Backup, BackupTable  # noqa: F401
from db_guardian.clones.models import Clone  # noqa: F401
