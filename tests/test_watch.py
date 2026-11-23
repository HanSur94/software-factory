"""Notifications for the owner (factory/watch.py)."""
import time
from datetime import datetime, timezone

from factory import watch


def _ts(minutes_ago):
    return datetime.fromtimestamp(time.time() - minutes_ago * 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _card(number, column, *, running=False, minutes_ago=1, detail="", step="Implementierer"):
    return {"repo": "o/game", "number": number, "column": column, "running": running, "detail": detail,
            "step": step, "title": f"Ticket {number}", "last": {"ts": _ts(minutes_ago)}}


def _alerts(cards, free_gb=100.0):
    return watch.alerts(cards, now=time.time(), stall_minutes=30, free_gb=free_gb, min_free_gb=15)


def test_a_stopped_and_a_waiting_ticket_are_reported():
    found = _alerts([_card(1, "stopped", detail="Budget aufgebraucht"), _card(2, "waiting", detail="Plan wartet")])
    titles = [title for _, title, _ in found]
    assert titles == ["game #1 angehalten", "game #2 wartet auf dich"]
    assert "Budget aufgebraucht" in found[0][2]


def test_a_running_ticket_without_progress_is_reported_after_the_stall_time():
    assert not _alerts([_card(3, "build", running=True, minutes_ago=10)])
    found = _alerts([_card(3, "build", running=True, minutes_ago=45)])
    assert found and found[0][1] == "game #3 hängt" and "45 Minuten" in found[0][2]


def test_finished_tickets_are_quiet():
    assert not _alerts([_card(4, "done"), _card(5, "review", minutes_ago=600)])


def test_low_disk_is_reported():
    found = _alerts([], free_gb=9.5)
    assert found and found[0][1] == "Wenig Speicher"


def test_each_alert_is_sent_once_and_a_new_cause_again():
    cards = [_card(1, "stopped", detail="x")]
    sent, seen = [], set()
    conf = {"stall_minutes": 30, "min_free_disk_gb": 0}
    watch.check_once(lambda: cards, conf, seen, send=lambda t, x: sent.append(t))
    watch.check_once(lambda: cards, conf, seen, send=lambda t, x: sent.append(t))
    assert sent == ["game #1 angehalten"]
    cards[0]["last"] = {"ts": _ts(0)}   # stopped again, later: a new event, a new alert
    watch.check_once(lambda: cards, conf, seen, send=lambda t, x: sent.append(t))
    assert len(sent) == 2
