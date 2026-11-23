"""The Claude plan usage on the board (factory/usage.py)."""
from factory import usage

ANSWER = {
    "five_hour": None,
    "seven_day": {"utilization": 3.0, "resets_at": "2026-11-25T19:00:00+00:00"},
    "seven_day_opus": None,
    "limits": [
        {"kind": "weekly_all", "percent": 3, "resets_at": "2026-11-25T19:00:00+00:00", "scope": None},
        {"kind": "weekly_scoped", "percent": 2, "resets_at": "2026-11-25T19:00:00+00:00", "is_active": False,
         "scope": {"model": {"display_name": "Fable"}}},
    ],
    "extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 1250.0, "currency": "EUR"},
}
NOW = 1_795_000_000.0


def test_claudes_answer_becomes_the_boards_windows():
    found = usage.windows(ANSWER, NOW)
    assert set(found) == {"seven_day", "seven_day_fable", "extra_usage"}   # empty windows are left out
    assert found["seven_day"]["used"] == 0.03 and found["seven_day"]["live"]
    assert found["seven_day"]["resets_at"] == 1_795_633_200.0
    assert found["seven_day_fable"]["model"] == "Fable" and found["seven_day_fable"]["active"] is False
    assert found["extra_usage"]["used"] == 0.25 and found["extra_usage"]["amount"] == "12.50 / 50.00 EUR"


def test_live_numbers_replace_what_agents_reported(monkeypatch):
    monkeypatch.setattr(usage, "_cache", {"at": 0.0, "value": None, "good": None, "until": 0.0})
    reported = {"seven_day": {"used": 0.61, "resets_at": NOW - 3600, "ts": "2026-11-18T10:00:00Z"}}
    current = usage.live(NOW, fetch=lambda: ANSWER)
    plan, source = usage.merge(reported, current, NOW)
    assert source == "live" and plan["seven_day"]["used"] == 0.03


def test_without_claude_stale_reported_windows_are_dropped(monkeypatch):
    monkeypatch.setattr(usage, "_cache", {"at": 0.0, "value": None, "good": None, "until": 0.0})
    reported = {"seven_day": {"used": 0.61, "resets_at": NOW - 3600},
                "five_hour": {"used": 0.20, "resets_at": NOW + 3600}}
    plan, source = usage.merge(reported, usage.live(NOW, fetch=lambda: None), NOW)
    assert source == "agents" and set(plan) == {"five_hour"}


def test_claude_is_asked_at_most_every_few_minutes(monkeypatch):
    monkeypatch.setattr(usage, "_cache", {"at": 0.0, "value": None, "good": None, "until": 0.0})
    calls = []
    fetch = lambda: calls.append(1) or ANSWER
    usage.live(NOW, fetch=fetch)
    usage.live(NOW + 30, fetch=fetch)
    usage.live(NOW + usage.CACHE_SECONDS + 1, fetch=fetch)
    assert len(calls) == 2


def test_a_failing_request_falls_back_quietly(monkeypatch):
    monkeypatch.setattr(usage, "_cache", {"at": 0.0, "value": None, "good": None, "until": 0.0})

    def broken():
        raise OSError("offline")
    assert usage.live(NOW, fetch=broken) is None


def test_a_rate_limit_keeps_the_last_answer_and_waits(monkeypatch):
    """The usage endpoint answered 429 and the board fell back to day-old agent reports."""
    from urllib.error import HTTPError
    monkeypatch.setattr(usage, "_cache", {"at": 0.0, "value": None, "good": None, "until": 0.0})
    first = usage.live(NOW, fetch=lambda: ANSWER)
    calls = []

    def limited():
        calls.append(1)
        raise HTTPError(usage.URL, 429, "Too Many Requests", {"Retry-After": "900"}, None)
    later = NOW + usage.CACHE_SECONDS + 1
    assert usage.live(later, fetch=limited) == first          # still the last live answer, with its own time
    assert usage.live(later + usage.CACHE_SECONDS + 1, fetch=limited) == first and len(calls) == 1   # waits 900 s
