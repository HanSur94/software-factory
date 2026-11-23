"""What the board's overview page shows besides the tickets: the trend, a feed and the machine's runners.

Everything here is worked out from what the board already reads, the tickets' events and the
orchestrator's records, so the overview costs no extra calls to GitHub."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

DAYS = 14
FEED = 30

# Events worth a line in the feed: (station, outcome) -> (text, tone). The tone colours the line.
NOTABLE = {
    ("pipeline", "started"): ("gestartet", "info"),
    ("plan_check", "pass"): ("Plan geprüft", "ok"),
    ("plan_check", "revise"): ("Plan muss überarbeitet werden", "warn"),
    ("review", "approve"): ("Review freigegeben", "ok"),
    ("review", "changes"): ("Review verlangt Änderungen", "warn"),
    ("judge", "pass"): ("Richter: bestanden", "ok"),
    ("judge", "fail"): ("Richter: durchgefallen", "bad"),
    ("ci", "fail"): ("CI rot", "bad"),
    ("checks", "fail"): ("Checks rot", "bad"),
    ("pipeline", "delivered"): ("Pull Request geöffnet", "ok"),
    ("pipeline", "merged"): ("automatisch gemergt", "done"),
    ("pipeline", "merge_held"): ("wartet auf deinen Merge", "warn"),
    ("pipeline", "split"): ("in Teil-Tickets aufgeteilt", "info"),
    ("pipeline", "stopped"): ("angehalten", "bad"),
    ("human", "merged"): ("gemergt", "done"),
    ("human", "closed"): ("geschlossen, ohne Merge", "info"),
    ("human", "asked"): ("Rückfrage gestellt", "warn"),
    ("human", "answered"): ("Rückfrage beantwortet", "ok"),
    ("guard", "over_limit"): ("Änderung zu groß", "warn"),
}
ORCHESTRATOR = {"restarted": "neu angestoßen", "started": "gestartet, Platz frei", "ci": "CI neu angestoßen"}


def _when(ts: str | None) -> datetime | None:
    try:
        return datetime.strptime(ts or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _day(ts: str | None) -> str | None:
    """The local calendar day of a UTC timestamp: the owner counts days on the board's clock."""
    when = _when(ts)
    return when.astimezone().date().isoformat() if when else None


def _tokens(counts: dict | None) -> int:
    return sum((counts or {}).get(k, 0) for k in ("input", "output", "cache_read", "cache_write"))


def merged(card: dict) -> bool:
    return card["column"] == "done" and "gemergt" in card["step"]


def finished_at(card: dict) -> str | None:
    """When a ticket was merged: its merge event, else its last event (a merge GitHub saw first)."""
    hits = [e["ts"] for e in card["workflow"]["timeline"] if e["outcome"] == "merged"]
    return hits[-1] if hits else (card.get("last") or {}).get("ts")


def reworked(card: dict) -> bool:
    """Whether a ticket needed a second go: the reviewer asked for changes or the judge failed it."""
    return any((e["station"], e["outcome"]) in (("review", "changes"), ("judge", "fail"))
               for e in card["workflow"]["timeline"])


def stats(cards: list[dict], now: datetime, days: int = DAYS) -> dict:
    """Merged tickets and tokens per day for the last days, and how tickets went through."""
    today = now.astimezone().date()
    keys = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    per = {k: {"day": k, "done": 0, "tokens": 0} for k in keys}
    leads, clean, done, rtk_saved = [], 0, 0, 0
    for c in cards:
        for station in c["workflow"]["stations"]:
            for run in station["runs"]:
                day = _day(run.get("ts"))
                if day in per:
                    per[day]["tokens"] += _tokens(run.get("tokens"))
                    rtk_saved += int((run.get("rtk") or {}).get("saved") or 0)
        if not merged(c) or c.get("parts"):   # a split ticket is counted through its parts
            continue
        end = finished_at(c)
        day = _day(end)
        if day in per:
            per[day]["done"] += 1
        done += 1
        clean += not reworked(c)
        start, stop = _when(c.get("started")), _when(end)
        if start and stop and stop > start:
            leads.append((stop - start).total_seconds() / 3600)
    leads.sort()
    median = leads[len(leads) // 2] if len(leads) % 2 else (sum(leads[len(leads) // 2 - 1:len(leads) // 2 + 1]) / 2 if leads else None)
    return {"days": list(per.values()), "merged": done, "clean": clean,
            "lead_hours": round(median, 1) if median is not None else None, "rtk_saved": rtk_saved}


def feed(cards: list[dict], projects: list[dict], limit: int = FEED) -> list[dict]:
    """The newest things that happened across all projects, newest first."""
    items = []
    for c in cards:
        for e in c["workflow"]["timeline"]:
            hit = NOTABLE.get((e["station"], e["outcome"]))
            if not hit or (e["station"] == "pipeline" and e["outcome"] == "started" and e["detail"] != "intake"):
                continue
            text, tone = hit
            if e["outcome"] == "split" and e["detail"]:
                text += ": " + e["detail"]
            items.append({"ts": e["ts"], "source": c["source"], "number": c["number"], "title": c["title"],
                          "text": text, "tone": tone})
    for p in projects:
        for sweep in (p.get("orchestrator") or {}).get("sweeps") or []:
            for d in sweep.get("decisions") or []:
                if d.get("action") in ORCHESTRATOR:
                    items.append({"ts": sweep.get("ts") or "", "source": p["source"], "number": d.get("ticket"),
                                  "title": "", "text": "Orchestrator: " + ORCHESTRATOR[d["action"]], "tone": "info"})
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items[:limit]


def runners(ps=None) -> dict | None:
    """GitHub's self-hosted runners on this machine: how many listen for jobs and how many run one.

    Read from the process list, not from GitHub, so it costs no API calls. None when it cannot be read."""
    try:
        out = ps() if ps else subprocess.run(["ps", "-axo", "command"], text=True, capture_output=True, timeout=5).stdout
    except Exception:
        return None
    lines = out.splitlines()
    online = sum("Runner.Listener" in line for line in lines)
    busy = sum("Runner.Worker" in line for line in lines)
    return {"online": online, "busy": min(busy, online)} if online else None


def build(cards: list[dict], projects: list[dict], now: datetime | None = None) -> dict:
    """The overview of every project together, and each project's own stats."""
    now = now or datetime.now(timezone.utc)
    for p in projects:
        p["stats"] = stats([c for c in cards if c["source"] == p["source"]], now)
    return {"stats": stats(cards, now), "feed": feed(cards, projects), "runners": runners()}
