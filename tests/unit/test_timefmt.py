from datetime import datetime

from db_guardian.timefmt import to_display, utcnow


def test_to_display_converts_utc_to_tz():
    assert to_display(datetime(2026, 11, 11, 20, 40), "America/Lima") == "2026-11-11T15:40:00-05:00"


def test_to_display_none():
    assert to_display(None, "UTC") is None


def test_utcnow_is_naive():
    assert utcnow().tzinfo is None
