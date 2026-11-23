"""Measurement and learning: numbers from the event log, and when an advisory
station has earned the right to block."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from . import events

POSITIVE = {"plan_check": "pass", "review": "approve", "judge": "pass"}


def collect(tickets_dir: Path) -> dict:
    tickets = {}
    if tickets_dir.is_dir():
        for d in sorted(tickets_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0):
            evs = events.read(d)
            if evs:
                tickets[d.name] = evs
    return tickets


def summarize(cfg: dict, tickets: dict) -> dict:
    per_station = defaultdict(lambda: {"runs": 0, "cost": 0.0, "seconds": 0.0})
    per_ticket = {}
    agreement = {s: {"agree": 0, "total": 0} for s in POSITIVE}
    for name, evs in tickets.items():
        cost = 0.0
        for e in evs:
            if e["kind"] == "agent":
                s = per_station[e["station"]]
                s["runs"] += 1
                s["cost"] += e.get("cost_usd", 0.0)
                s["seconds"] += e.get("duration_s", 0.0)
                cost += e.get("cost_usd", 0.0)
            if e["station"] == "judge" and e["kind"] == "verdict":
                cost += e.get("cost_usd", 0.0)
        human = [e["outcome"] for e in evs if e["kind"] == "human" and e.get("outcome") in ("merged", "closed")]
        per_ticket[name] = {"cost": round(cost, 2), "outcome": human[-1] if human else "offen"}
        if human:
            for station, good in POSITIVE.items():
                verdicts = [e["outcome"] for e in evs if e["station"] == station and e["kind"] == "verdict"]
                if verdicts and verdicts[-1] != "none":
                    agreement[station]["total"] += 1
                    agreement[station]["agree"] += int((verdicts[-1] == good) == (human[-1] == "merged"))
    rule = cfg["pipeline"]["promote_after"]
    promote = {}
    for station, a in agreement.items():
        rate = a["agree"] / a["total"] if a["total"] else None
        promote[station] = {
            "mode": cfg["pipeline"]["modes"].get(station, "gate"), "tickets": a["total"], "agreement": rate,
            "ready": rate is not None and a["total"] >= rule["tickets"] and rate >= rule["agreement"],
        }
    return {"stations": dict(per_station), "tickets": per_ticket, "promotion": promote, "rule": rule}


def render(summary: dict) -> str:
    lines = ["Kosten je Station", "  Station        Läufe   Kosten   Minuten"]
    for station, s in sorted(summary["stations"].items()):
        lines.append(f"  {station:<14} {s['runs']:>5} {s['cost']:>7.2f}$ {s['seconds'] / 60:>8.1f}")
    lines += ["", "Tickets", "  Ticket   Kosten   Ausgang"]
    for name, t in summary["tickets"].items():
        lines.append(f"  #{name:<6} {t['cost']:>6.2f}$   {t['outcome']}")
    rule = summary["rule"]
    lines += ["", f"Beratende Stationen (Tor ab {rule['agreement']:.0%} Übereinstimmung über {rule['tickets']} Tickets)"]
    for station, p in summary["promotion"].items():
        rate = "-" if p["agreement"] is None else f"{p['agreement']:.0%}"
        hint = "  -> kann auf gate gestellt werden" if p["ready"] and p["mode"] == "advisory" else ""
        lines.append(f"  {station:<11} Modus {p['mode']:<9} Übereinstimmung {rate:>4} über {p['tickets']} Tickets{hint}")
    return "\n".join(lines)
