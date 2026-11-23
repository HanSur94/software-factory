"""The time a ticket spent per lane, for the board's timetable and signal box."""
from factory import flowlog


def ev(ts, station, kind="agent", outcome="ok"):
    return {"ts": f"2026-11-22T{ts}:00Z", "station": station, "kind": kind, "outcome": outcome}


def test_work_waiting_rework_and_merge_become_lanes():
    events = [ev("08:00", "pipeline", "pipeline", "queued"), ev("09:00", "pipeline", "pipeline", "started"),
              ev("09:10", "planner"), ev("10:00", "implementer"), ev("10:30", "checks", "verdict", "pass"),
              ev("11:00", "judge", "verdict", "fail"), ev("11:20", "pipeline", "pipeline", "started"),
              ev("11:50", "rework"), ev("12:10", "judge", "verdict", "pass"), ev("12:15", "pipeline", "pipeline", "delivered"),
              ev("12:40", "ci", "verdict", "pass"), ev("14:00", "human", "human", "merged"),
              ev("14:05", "ci", "verdict", "pass")]
    got = flowlog.lanes(events, "done", "merged", "2026-11-22T15:00:00Z")
    assert [l[0] for l in got["lanes"]] == ["queue", "plan", "build", "verify", "queue", "rework", "verify", "pr"]
    assert got["lanes"][-1][2] == "2026-11-22T14:00:00Z"   # the CI after the merge is no work on it
    assert got["end"] == ["done", "2026-11-22T14:00:00Z"]


def test_an_open_ticket_runs_until_now_in_its_column():
    events = [ev("09:00", "pipeline", "pipeline", "started"), ev("09:10", "planner"),
              ev("09:20", "human", "human", "asked")]
    got = flowlog.lanes(events, "waiting", "plan", "2026-11-22T10:00:00Z")
    assert got["lanes"][-1] == ["wait", "2026-11-22T09:20:00Z", "2026-11-22T10:00:00Z"]
    building = flowlog.lanes(events[:2], "build", "rework_judge", "2026-11-22T10:00:00Z")
    assert building["lanes"][-1][0] == "rework" and building["end"] is None


def test_a_split_parent_stops_at_the_split():
    events = [ev("09:00", "pipeline", "pipeline", "started"), ev("09:10", "planner"),
              ev("09:11", "pipeline", "pipeline", "split"), ev("09:12", "pipeline", "pipeline", "stopped")]
    got = flowlog.lanes(events, "build", "split", "2026-11-22T10:00:00Z")
    assert got["end"] == ["split", "2026-11-22T09:11:00Z"] and got["lanes"][-1][2] == "2026-11-22T09:11:00Z"


def test_runs_on_named_runners_become_bays():
    stations = [{"id": "implementer", "runs": [{"runner": "Mac-factory-2", "ts": "2026-11-22T10:00:00Z", "seconds": 600},
                                               {"runner": "", "ts": "2026-11-22T11:00:00Z", "seconds": 60}]}]
    assert flowlog.bays(stations) == [["Mac-factory-2", "2026-11-22T09:50:00Z", "2026-11-22T10:00:00Z", "implementer"]]
