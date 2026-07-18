from datetime import UTC, datetime

from app.feedgen import _duration, _rfc2822


def test_duration_formats_hms():
    assert _duration(3661) == "01:01:01"
    assert _duration(0) == "00:00:00"
    assert _duration(59) == "00:00:59"
    assert _duration(3600) == "01:00:00"


def test_rfc2822_aware_datetime():
    got = _rfc2822(datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC))
    assert "17 Jul 2026 12:00:00" in got
    assert got.endswith("+0000")


def test_rfc2822_naive_datetime_treated_as_utc():
    got = _rfc2822(datetime(2026, 7, 17, 12, 0, 0))
    assert "17 Jul 2026 12:00:00 +0000" in got


def test_rfc2822_none_returns_a_string():
    assert isinstance(_rfc2822(None), str) and _rfc2822(None)
