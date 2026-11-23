"""The overview page's numbers: merged per day, tokens per day, rework, the feed and the runners."""

from datetime import datetime, timezone

from factory import overview

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def ticket(number, column="build", step="Bau", events=(), runs=(), started=None, parts=None, last=None):
    timeline = [{"ts": ts, "station": st, "kind": "x", "outcome": out, "detail": detail} for ts, st, out, detail in events]
    return {"source": "gh:o/r", "number": number, "title": f"T{number}", "column": column, "step": step, "parts": parts,
            "started": started, "last": {"ts": last} if last else None,
            "workflow": {"timeline": timeline,
                         "stations": [{"runs": [{"ts": ts, "tokens": {"input": n, "output": 0}} for ts, n in runs]}]}}


def test_merged_tickets_and_tokens_land_on_their_day():
    cards = [
        ticket(1, "done", "gemergt", events=[("2026-11-18T10:00:00Z", "human", "merged", "")],
               runs=[("2026-11-18T08:00:00Z", 100), ("2026-11-19T09:00:00Z", 50)], started="2026-11-18T06:00:00Z"),
        ticket(2, "done", "automatisch gemergt", events=[("2026-11-19T11:00:00Z", "review", "changes", ""),
                                                         ("2026-11-19T11:30:00Z", "pipeline", "merged", "")],
               started="2026-11-19T07:30:00Z"),
        ticket(3, "done", "verworfen", events=[("2026-11-19T11:00:00Z", "human", "closed", "")]),
        ticket(4, "done", "gemergt", parts=[5, 6], last="2026-11-19T11:00:00Z"),   # counted through its parts
    ]
    got = overview.stats(cards, NOW, days=3)
    by_day = {d["day"]: d for d in got["days"]}
    assert [d["day"] for d in got["days"]] == ["2026-11-17", "2026-11-18", "2026-11-19"]
    assert by_day["2026-11-18"]["done"] == 1 and by_day["2026-11-19"]["done"] == 1
    assert by_day["2026-11-18"]["tokens"] == 100 and by_day["2026-11-19"]["tokens"] == 50
    assert got["merged"] == 2 and got["clean"] == 1          # #2 needed a second go
    assert got["lead_hours"] == 4.0                          # median of 4 h and 4 h


def test_feed_keeps_notable_events_newest_first_with_the_orchestrator():
    cards = [ticket(7, events=[("2026-11-19T09:00:00Z", "pipeline", "started", "intake"),
                               ("2026-11-19T09:05:00Z", "pipeline", "started", "planner"),   # a station, not the ticket
                               ("2026-11-19T09:10:00Z", "implementer", "ok", ""),            # not notable
                               ("2026-11-19T10:00:00Z", "pipeline", "split", "#8, #9")])]
    projects = [{"source": "gh:o/r", "orchestrator": {"sweeps": [
        {"ts": "2026-11-19T11:00:00Z", "decisions": [{"ticket": 5, "action": "restarted"}, {"ticket": 6, "action": "left"}]}]}}]
    feed = overview.feed(cards, projects)
    assert [(i["number"], i["text"]) for i in feed] == [
        (5, "Orchestrator: neu angestoßen"), (7, "in Teil-Tickets aufgeteilt: #8, #9"), (7, "gestartet")]


def test_runners_are_read_from_the_process_list():
    ps = "\n".join(["/a/bin/Runner.Listener run", "/b/bin/Runner.Listener run", "/a/bin/Runner.Worker spawnclient", "zsh"])
    assert overview.runners(lambda: ps) == {"online": 2, "busy": 1}
    assert overview.runners(lambda: "zsh") is None


def test_build_gives_every_project_its_own_stats():
    projects = [{"source": "gh:o/r"}, {"source": "local:0"}]
    got = overview.build([ticket(1, "done", "gemergt", events=[("2026-11-19T10:00:00Z", "human", "merged", "")])], projects, NOW)
    assert projects[0]["stats"]["merged"] == 1 and projects[1]["stats"]["merged"] == 0
    assert got["stats"]["merged"] == 1 and "feed" in got and "runners" in got
