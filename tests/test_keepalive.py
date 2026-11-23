"""The board starts the orchestrator when GitHub drops its schedule (factory/keepalive.py)."""
from factory import keepalive

NOW = 1_795_000_000.0


def _tick(state, last_run_minutes_ago, answer="ok"):
    started = []
    found = keepalive.tick(["o/game"], state, NOW, 40, last=lambda repo: NOW - last_run_minutes_ago * 60,
                           start=lambda repo: started.append(repo) or answer)
    return found, started


def test_a_recent_scheduled_run_needs_no_help():
    assert _tick({}, 10) == ([], [])


def test_a_dropped_schedule_is_replaced_once():
    state = {}
    assert _tick(state, 300) == (["o/game"], ["o/game"])
    assert _tick(state, 300) == ([], [])          # started here a moment ago


def test_a_workflow_without_the_input_is_left_alone():
    state = {}
    assert _tick(state, 300, answer="unsupported") == ([], ["o/game"])
    assert _tick(state, 300) == ([], [])          # not asked again
