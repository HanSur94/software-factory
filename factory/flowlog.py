"""Where a ticket spent its time: its events turned into stretches per lane of the line.

The board's line views (timetable, signal box) draw these. A stretch is [lane, start, end]. Lanes are what a
person reads on the line: queue (waits for a free place), plan, build, rework, verify, wait (waits for the
owner), pr (pull request open), stopped. Agent and check events mark the end of the work stretch they close;
pipeline and human events open or close the stretches where nothing works on the ticket."""
from __future__ import annotations

import re

LANE_OF = {"intake": "plan", "planner": "plan", "plan_check": "plan", "triage": "plan",
           "implementer": "build", "rework": "rework",
           "checks": "verify", "reviewer": "verify", "review": "verify", "judge": "verify", "guard": "verify",
           "evidence": "verify", "ci": "verify"}
# What the ticket does after an event, when that is a stretch where nothing works on it.
HOLD = {("pipeline", "queued"): "queue", ("pipeline", "delivered"): "pr", ("pipeline", "stopped"): "stopped",
        ("human", "asked"): "wait"}
RESUME = {("pipeline", "started"), ("pipeline", "auto_restart"), ("human", "answered")}
END = {("pipeline", "split"): "split", ("pipeline", "merged"): "done", ("human", "merged"): "done", ("human", "closed"): "done"}
# Where the ticket stands now, by its board column, for the stretch from its last event until now.
COLUMN_LANE = {"queue": "queue", "plan": "plan", "build": "build", "verify": "verify", "waiting": "wait",
               "stopped": "stopped", "review": "pr"}


def lanes(events: list[dict], column: str, phase: str | None, now: str) -> dict:
    """{"lanes": [[lane, start, end], ...], "end": [how, ts] | None} for one ticket, oldest first."""
    out: list[list] = []
    hold, since, ended = None, None, None

    def add(lane, start, end):
        if not lane or not start or not end or end <= start:
            return
        if out and out[-1][0] == lane and out[-1][2] == start:
            out[-1][2] = end
        else:
            out.append([lane, start, end])

    for e in sorted((e for e in events if e.get("ts")), key=lambda e: e["ts"]):
        kind, ts = e.get("kind"), e["ts"]
        k = (("pipeline" if kind == "pipeline" else e.get("station")), e.get("outcome", "")) if kind in ("pipeline", "human") else None
        last = out[-1][0] if out else "queue"
        if ended and k not in RESUME:
            continue   # merged or split: what follows (the project's CI, a stop note of the parent) is no work on it
        if k in END:
            add(hold or last, since, ts)
            ended, hold, since = [END[k], ts], None, ts
            continue
        if k in HOLD:
            add(hold or last, since, ts)   # handing over after work still belongs to that work
            hold, since, ended = HOLD[k], ts, None
            continue
        if k in RESUME:
            add(hold or "queue", since, ts)
            hold, since, ended = None, ts, None
            continue
        lane = LANE_OF.get(e.get("station") or "") if kind != "pipeline" else None
        if not lane:
            continue   # notes of the pipeline (floorplan, resources, base merged …) are no boundary
        if hold:
            continue   # the project's CI on an open pull request: still the owner's turn
        add(lane, since, ts)
        since = ts
    if since and not ended:
        lane = hold or COLUMN_LANE.get(column)
        if lane == "build" and str(phase or "").startswith("rework"):
            lane = "rework"
        add(lane, since, now)
    return {"lanes": out, "end": ended}


RUNNER = re.compile(r"^\S+$")


def bays(stations: list[dict]) -> list[list]:
    """The runs of a ticket on named runners: [[runner, start, end, station], ...]; to see who held a place when."""
    from datetime import datetime, timedelta, timezone
    out = []
    for st in stations:
        for r in st.get("runs") or []:
            name, ts, secs = r.get("runner") or "", r.get("ts"), float(r.get("seconds") or 0)
            if not name or not ts or secs <= 0 or not RUNNER.match(name):
                continue
            try:
                end = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            start = (end - timedelta(seconds=secs)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append([name, start, ts, st.get("id", "")])
    return sorted(out, key=lambda b: b[1])
