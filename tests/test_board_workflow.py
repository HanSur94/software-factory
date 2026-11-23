"""The stations a ticket shows on the board (factory/board.py)."""
from factory import board


def _event(station, kind="verdict", outcome="pass"):
    return {"ts": "2026-11-19T20:35:00Z", "station": station, "kind": kind, "outcome": outcome}


def test_a_split_ticket_shows_no_station_at_work():
    """A ticket split into parts, the board still said the plan check was running."""
    events = [_event("intake"), _event("planner", "agent"), _event("plan_check"),
              _event("pipeline", "pipeline", "split")]
    flow = board.workflow({"phase": "split"}, {}, None, events, {}, None)
    states = {s["id"]: s["state"] for s in flow["stations"]}
    assert states["plan_check"] == "done"
    assert "active" not in states.values()
    assert all(states[s] == "skipped" for s in ("implementer", "checks", "review", "judge", "deliver"))


def test_a_finished_ticket_without_pull_request_waits_for_no_ci():
    assert board.ci_station({"column": "done"}, None)["state"] == "skipped"
    assert board.ci_station({"column": "build"}, None)["state"] == "pending"


def _split_card(number, parts):
    events = "\n".join([
        '{"ts": "2026-11-19T20:35:00Z", "station": "plan_check", "kind": "verdict", "outcome": "pass"}',
        '{"ts": "2026-11-19T20:36:00Z", "station": "pipeline", "kind": "pipeline", "outcome": "split", "detail": "'
        + ", ".join(f"#{n}" for n in parts) + '"}'])
    return board.card("o/game", number, {"status.json": '{"phase": "split"}', "events.jsonl": events}, None, None)


def test_a_split_ticket_is_in_progress_until_its_parts_are_done():
    """A ticket stood under Fertig right after it was split, with none of its parts built."""
    parent = _split_card(10, [11, 12])
    first = board.card("o/game", 11, {"status.json": '{"phase": "merged"}'}, None, None)
    second = board.card("o/game", 12, {}, {"title": "T2", "url": "u", "labels": ["factory:part", "factory:blocked"]}, None)
    board.follow_parts([parent, first, second])
    assert parent["column"] == "build" and parent["step"] == "in Teil-Tickets: 1 von 2 fertig"
    assert not parent["restartable"]
    assert second["step"] == "wartet auf die Teile, von denen es abhängt"
    assert board.ci_station(parent, None)["state"] == "skipped"

    second_done = board.card("o/game", 12, {"status.json": '{"phase": "merged"}'}, None, None)
    parent = _split_card(10, [11, 12])
    board.follow_parts([parent, first, second_done])
    assert parent["column"] == "done" and parent["step"] == "alle 2 Teile fertig"


def test_a_ticket_whose_pull_request_is_closed_no_longer_waits_for_a_merge():
    """Tickets waited for a merge of pull requests closed long ago."""
    waiting = board.card("o/game", 7, {"status.json": '{"phase": "delivered", "pr": "https://github.com/o/game/pull/9"}'},
                         None, None)
    assert waiting["column"] == "review"
    board.closed_pr(waiting, {"pr_state": "OPEN"})
    assert waiting["column"] == "review"
    board.closed_pr(waiting, {"pr_state": "CLOSED"})
    assert waiting["column"] == "done" and waiting["step"] == "Pull Request geschlossen, ohne Merge"


def test_a_pull_request_with_a_merge_conflict_says_so_on_its_card():
    waiting = {"column": "review", "step": "wartet auf deinen Merge", "detail": ""}
    board.conflicted_pr(waiting, {"pr_state": "OPEN", "conflict": False, "base": "develop"})
    assert "conflict" not in waiting
    board.conflicted_pr(waiting, {"pr_state": "OPEN", "conflict": True, "base": "develop"})
    assert waiting["conflict"] and waiting["step"] == "Merge-Konflikt mit develop"
    assert waiting["detail"].startswith("GitHub kann diesen Pull Request nicht mergen: develop")
    building = {"column": "build", "step": "Implementierer", "detail": ""}
    board.conflicted_pr(building, {"pr_state": "OPEN", "conflict": True, "base": "develop"})
    assert "conflict" not in building   # only a pull request waiting for its merge
