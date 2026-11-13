"""Compare a backup's tables with a clone target's tables (pure functions)."""

import re
from dataclasses import dataclass

from db_guardian.mysqlclient.dump import parse_version

_AUTO_INCREMENT_OPTION = re.compile(r"\s+AUTO_INCREMENT=\d+")
_WHITESPACE = re.compile(r"\s+")
_COLLATION_0900 = re.compile(r"utf8mb4_0900_\w+")


def normalize_create_sql(sql: str) -> str:
    """Drop the AUTO_INCREMENT=<n> table option (a counter, not structure) and collapse whitespace."""
    without_counter = _AUTO_INCREMENT_OPTION.sub("", sql)
    return _WHITESPACE.sub(" ", without_counter).strip()


@dataclass
class SchemaDiff:
    missing_in_target: list[str]
    structure_differs: list[str]
    only_in_target: list[str]
    matching: list[str]


def diff_schema(backup: dict[str, str], target: dict[str, str]) -> SchemaDiff:
    """Both arguments map table name -> CREATE TABLE text."""
    common = backup.keys() & target.keys()
    differs = [
        name for name in common
        if normalize_create_sql(backup[name]) != normalize_create_sql(target[name])
    ]
    return SchemaDiff(
        missing_in_target=sorted(backup.keys() - target.keys()),
        structure_differs=sorted(differs),
        only_in_target=sorted(target.keys() - backup.keys()),
        matching=sorted(common - set(differs)),
    )


def _is_mariadb(version: str) -> bool:
    return "mariadb" in version.lower()


def version_warnings(source_version: str | None, target_version: str, backup_sql: list[str]) -> list[str]:
    """Warnings about server versions that make the restore likely to fail."""
    if not source_version:
        return []
    warnings = []
    same_engine = _is_mariadb(source_version) == _is_mariadb(target_version)
    if same_engine and parse_version(target_version) < parse_version(source_version):
        warnings.append(
            f"target server {target_version} is older than the backup's server {source_version}; "
            "the restore may fail"
        )
    old_target = _is_mariadb(target_version) or parse_version(target_version) < (8, 0)
    if old_target:
        collations = sorted({m for sql in backup_sql for m in _COLLATION_0900.findall(sql)})
        if collations:
            warnings.append(
                f"backup uses collation {', '.join(collations)}, which {target_version} does not "
                "support; the restore will likely fail"
            )
    return warnings
