import pytest
from pydantic import ValidationError

from db_guardian.sources.schemas import SourceCreate, SourceUpdate


def valid(**overrides):
    data = {
        "name": "intranet",
        "host": "10.0.0.5",
        "port": 3306,
        "user": "backup",
        "password": "secret",
        "database": "intranet",
        "tables": ["*"],
        "schedule": "0 3 * * *",
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize("tables", [[], ["*", "users"], ["  "], ["users", ""]])
def test_invalid_tables_rejected(tables):
    with pytest.raises(ValidationError):
        SourceCreate(**valid(tables=tables))


def test_star_and_list_accepted():
    assert SourceCreate(**valid(tables=["*"])).tables == ["*"]
    assert SourceCreate(**valid(tables=[" hist_a ", "hist_b"])).tables == ["hist_a", "hist_b"]


def test_invalid_cron_rejected():
    with pytest.raises(ValidationError):
        SourceCreate(**valid(schedule="99 * * * *"))


def test_retention_min_1():
    with pytest.raises(ValidationError):
        SourceCreate(**valid(retention=0))


def test_update_validates_tables_too():
    with pytest.raises(ValidationError):
        SourceUpdate(tables=[])
