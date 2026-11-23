"""The orchestrator: looks after every ticket of a project, so the owner does not have to.

Two halves. The gate (slot_reason, start_queued) runs inside every ticket run: a ticket
starts only while there is a free slot (limits.max_parallel_tickets) and money left for
the day (limits.daily_budget_usd); otherwise it waits with the label factory:queued, and
every run that ends hands its slot to the oldest waiting ticket. The sweep (sweep) runs on
a schedule in the project's caller workflow and repairs what no event reports:

- a ticket whose run broke off (a runner that died, a cancelled run) is started again;
- a ticket stopped by a fault of the machinery rather than of the work is started again;
- a delivered ticket whose CI result never arrived is handled as if it had (a branch
  older than the CI relay, a lost dispatch);
- a delivered ticket whose pull request GitHub reports as conflicting with its base goes back
  for one round: the base is merged in, the conflict resolved, checks and judge run again
  (once per commit of the base, and only while a slot and money for the day are free);
- waiting tickets get the slots that are free.

Restarts are counted per ticket (orchestrator.max_auto_restarts); after that a ticket
stays stopped for the owner.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from . import events

# A stop with one of these in its reason was the machinery, not the work: worth one more try.
INFRASTRUCTURE = ("unerwarteter Fehler im Steuerskript", "could not push state", "GitError", "HostError",
                  "timed out", "Connection reset", "rate limit", "502", "503", "zu wenig freier Speicher")
ACTIVE_PHASES = ("intake", "plan", "plan_check", "implement", "checks", "rework_judge", "rework_ci", "rework_base",
                 "judge")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_minutes(ts: str | None) -> float:
    if not ts:
        return float("inf")
    return (_now() - datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 60


def spent_today(tickets_dir: Path) -> float:
    """What every ticket of the project spent today (UTC), from the events on the state branch."""
    today, total = _now().strftime("%Y-%m-%d"), 0.0
    for d in Path(tickets_dir).glob("*"):
        for e in events.read(d) if d.is_dir() else []:
            if str(e.get("ts", "")).startswith(today):
                total += float(e.get("cost_usd") or 0.0)
    return round(total, 4)


def tokens_today(tickets_dir: Path) -> int:
    """Every token the project's agents used today (UTC): with a subscription, the measure that counts."""
    today, total = _now().strftime("%Y-%m-%d"), 0
    for d in Path(tickets_dir).glob("*"):
        for e in events.read(d) if d.is_dir() else []:
            if str(e.get("ts", "")).startswith(today):
                total += sum(int(v or 0) for v in (e.get("tokens") or {}).values())
    return total


def slot_reason(ctx) -> str | None:
    """Why this ticket may not start now; None when it may."""
    limits = ctx.cfg["limits"]
    daily, wip = float(limits.get("daily_budget_usd") or 0), int(limits.get("max_parallel_tickets") or 0)
    if daily:
        spent = spent_today(ctx.store.tickets_dir())
        if spent >= daily:
            return f"das Tagesbudget von {daily:.2f} $ ist verbraucht ({spent:.2f} $), weiter geht es morgen (UTC)"
    if wip:
        running = [i for i in ctx.host.open_issues(ctx.label("running")) if i["number"] != ctx.ticket]
        if len(running) >= wip:
            return f"alle {wip} Plätze sind belegt"
    return None


def queue(ctx, reason: str) -> str:
    st, host, L = ctx.status, ctx.host, ctx.label
    first = not st.get("queued")
    st["queued"] = reason
    host.labels(add=[L("queued")], remove=[L("go"), L("running")])
    if first:
        host.comment(f"**Wartet auf einen freien Platz:** {reason}. Die Factory startet das Ticket selbst, sobald es geht.")
    ctx.emit("pipeline", "pipeline", outcome="queued", detail=reason[:200])
    ctx.save("queued")
    return "queued"


def start_queued(ctx, decisions: list | None = None) -> list[int]:
    """Hand free slots to the oldest waiting tickets. Called when a run ends and by the sweep.

    decisions, when given, gets one entry per ticket started, with why it could start."""
    limits, host, L = ctx.cfg["limits"], ctx.host, ctx.label
    waiting = [i["number"] for i in host.open_issues(L("queued"))]
    if not waiting:
        return []
    daily, wip = float(limits.get("daily_budget_usd") or 0), int(limits.get("max_parallel_tickets") or 0)
    if daily and spent_today(ctx.store.tickets_dir()) >= daily:
        return []
    free = len(waiting)
    if wip:
        free = max(0, wip - len(host.open_issues(L("running"))))
    started = waiting[:free]
    taken = len(host.open_issues(L("running"))) if wip else 0
    for i, number in enumerate(started):
        host.dispatch(number)
        print(f"[factory] #{number} gets a free slot", flush=True)
        if decisions is not None:
            decisions.append({"ticket": number, "action": "started",
                              "why": f"Platz frei ({taken + i + 1} von {wip})" if wip else "kein Platzlimit"})
    return started


def _stuck(status: dict, last_ts: str | None, labels: set, active: list[dict], stale: int, running_label: str) -> bool:
    """Its run broke off: it says it is at work, but no run of the workflow is."""
    if status.get("stopped") or status.get("waiting") or status.get("queued"):
        return False
    if status.get("phase") not in ACTIVE_PHASES or running_label not in labels:
        return False
    if _age_minutes(last_ts) < stale:
        return False
    mine = {u.rstrip("/").rsplit("/", 1)[-1] for u in status.get("run_urls") or []}
    if any(r["id"] in mine for r in active):
        return False
    # A run that has not written its URL yet could be this ticket's: decide next time.
    return not any(r["id"] not in mine and (r["created_at"] or "") >= (last_ts or "") for r in active)


def _infrastructure(status: dict) -> bool:
    reason = str(status.get("stopped") or "")
    return bool(reason) and any(mark in reason for mark in INFRASTRUCTURE)


def _slots(ctx) -> int:
    return int(ctx.cfg["limits"].get("max_parallel_tickets") or 0)


def _room(ctx, running: int) -> list:
    """[why nothing may start now or None, how many tickets may still start or None for no limit]: the same rules
    as slot_reason, counted once for the whole sweep."""
    daily = float(ctx.cfg["limits"].get("daily_budget_usd") or 0)
    if daily:
        spent = spent_today(ctx.store.tickets_dir())
        if spent >= daily:
            return [f"das Tagesbudget von {daily:.2f} $ ist verbraucht ({spent:.2f} $)", 0]
    wip = _slots(ctx)
    if not wip:
        return [None, None]
    free = wip - running
    return [None if free > 0 else f"alle {wip} Plätze sind belegt", max(0, free)]


def _rework_conflict(ctx, ticket_ctx, number: int, status: dict, room: list, done: dict, decisions: list) -> bool:
    """A delivered pull request GitHub cannot merge because its base changed the same lines: send it back for one
    round (deliver.base_conflict). Once per commit of the base; a conflict that is still there on the same base
    stays for the owner. True when the ticket was sent back."""
    from . import deliver

    branch = status.get("base_branch")
    base = ctx.host.branch_head(branch) if branch else None
    if not base or base == status.get("conflict_base"):
        return False
    if room[0] is not None:
        decisions.append({"ticket": number, "action": "left", "why": f"Merge-Konflikt mit {branch}, aber {room[0]}"})
        return False
    if deliver.base_conflict(ticket_ctx(number), base) != "rework":
        return False
    if room[1] is not None:
        room[1] -= 1
        if room[1] <= 0:
            room[0] = f"alle {_slots(ctx)} Plätze sind belegt"
    done["conflicts"].append(number)
    decisions.append({"ticket": number, "action": "restarted",
                      "why": f"Merge-Konflikt mit {branch}: arbeitet {base[:7]} ein, danach prüfen Checks und Richter neu"})
    return True


def sweep(ctx, ticket_ctx) -> dict:
    """One look at every ticket. ticket_ctx(number) gives the context of one ticket to act on it."""
    from . import deliver

    cfg, host, L = ctx.cfg, ctx.host, ctx.label
    limit = int((cfg.get("orchestrator") or {}).get("max_auto_restarts", 2))
    stale = int((cfg.get("orchestrator") or {}).get("stale_minutes", 20))
    active = host.active_runs()
    running = {i["number"] for i in host.open_issues(L("running"))}
    aborted = {i["number"] for i in host.open_issues(L("aborted"))} if "aborted" in cfg["human"]["labels"] else set()
    done: dict[str, list] = {"restarted": [], "ci": [], "conflicts": [], "started": [], "left": []}
    decisions: list[dict] = []   # what the sweep did, and why: shown on the board
    room = _room(ctx, len(running))   # slots a conflicted pull request may take in this sweep
    checked = 0
    for d in sorted(Path(ctx.store.tickets_dir()).glob("*")):
        if not (d.is_dir() and d.name.isdigit() and (d / "status.json").exists()):
            continue
        number, status = int(d.name), json.loads((d / "status.json").read_text())
        if number in aborted:
            continue   # aborted by hand: no restart, no CI follow-up, no merge
        evs = events.read(d)
        last_ts = evs[-1]["ts"] if evs else None
        checked += 1
        labels = {L("running")} if number in running else set()

        just_restarted = bool(evs) and evs[-1].get("outcome") == "auto_restart" and _age_minutes(last_ts) < stale
        if not just_restarted and (_stuck(status, last_ts, labels, active, stale, L("running")) or
                                   (status.get("stopped") and _infrastructure(status))):
            t = ticket_ctx(number)
            if t.status.get("auto_restarts", 0) >= limit:
                done["left"].append(number)
                decisions.append({"ticket": number, "action": "left",
                                  "why": f"hängt, hatte aber schon {limit} automatische Neustarts: bleibt für dich"})
                continue
            t.status["auto_restarts"] = t.status.get("auto_restarts", 0) + 1
            why = "angehalten durch einen Fehler der Maschinerie" if t.status.get("stopped") else \
                f"seit {int(_age_minutes(last_ts))} Minuten ohne Lauf"
            t.host.comment(f"**Orchestrator:** Das Ticket war {why} und läuft neu an "
                           f"(automatischer Neustart {t.status['auto_restarts']} von {limit}).")
            t.emit("pipeline", "pipeline", outcome="auto_restart", detail=why)
            t.save("restarted by the orchestrator")
            t.host.dispatch(number)
            done["restarted"].append(number)
            decisions.append({"ticket": number, "action": "restarted",
                              "why": f"{why} (Neustart {t.status['auto_restarts']} von {limit})"})

        elif status.get("phase") == "delivered" and str(status.get("pr") or "").startswith("http"):
            pr = host.pull_request(status["pr"]) or {}
            sha = pr.get("head")
            if not sha:
                continue
            if pr.get("conflict") and _rework_conflict(ctx, ticket_ctx, number, status, room, done, decisions):
                continue   # its run takes in the base: a CI result of the old head does not count any more
            seen = " ".join(str(e.get("detail", "")) for e in evs if e.get("station") == "ci")
            state = host.checks_state(sha)
            if state == "red":
                failed = host.failed_check(sha)
                if failed and host.run_url(failed[1]) not in seen:   # each red run is acted on once
                    outcome = deliver.ci_result(ticket_ctx(number), failed[0], "failure", failed[1], sha)
                    done["ci"].append((number, outcome))
                    decisions.append({"ticket": number, "action": "ci",
                                      "why": f"CI-Ergebnis nachgeholt: {failed[0]} rot, {outcome}"})
            elif state == "green" and cfg["delivery"].get("auto_merge") and "merge_held" not in \
                    {e.get("outcome") for e in evs[-1:]}:
                outcome = deliver.ci_result(ticket_ctx(number), "CI", "success", "", sha)
                if outcome == "merged":
                    done["ci"].append((number, outcome))
                    decisions.append({"ticket": number, "action": "ci", "why": "CI-Ergebnis nachgeholt: grün, gemergt"})

    done["started"] = start_queued(ctx, decisions)
    done["decisions"], done["checked"] = decisions, checked
    return done


# ---- what the orchestrator did, for the board --------------------------------------

KEEP = 100   # records kept on the state branch


def capacity(ctx) -> dict:
    """How full the project is: slots, waiting tickets and what today used."""
    limits, host, L = ctx.cfg["limits"], ctx.host, ctx.label
    tickets = ctx.store.tickets_dir()
    return {"running": len(host.open_issues(L("running"))), "slots": int(limits.get("max_parallel_tickets") or 0),
            "queued": [{"ticket": i["number"], "since": i["created_at"]} for i in host.open_issues(L("queued"))],
            "tokens_today": tokens_today(tickets), "spent_today": spent_today(tickets),
            "daily_budget": float(limits.get("daily_budget_usd") or 0)}


def trigger() -> str:
    """What started this run, in the words of the board."""
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    return {"schedule": "Zeitplan von GitHub", "workflow_dispatch": "angestoßen"}.get(event, event or "von Hand")


def record(ctx, trigger_text: str, decisions: list, checked: int = 0, started_at: float | None = None,
           helpers: dict | None = None, prune: bool = False) -> None:
    """One file per run on the state branch, so parallel writers never touch the same file."""
    folder = Path(ctx.store.tickets_dir()).parent / "orchestrator" / "sweeps"
    folder.mkdir(parents=True, exist_ok=True)
    now = _now()
    server, repo, run = (os.environ.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    entry = {"schema": "sweep/1", "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "trigger": trigger_text,
             "seconds": round(time.time() - started_at, 1) if started_at else None,
             "run_url": f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None,
             "decisions": decisions, "checked": checked, "capacity": capacity(ctx), "helpers": helpers or {}}
    name = now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3) + ".json"
    (folder / name).write_text(json.dumps(entry, ensure_ascii=False, indent=1) + "\n")
    if prune:   # only the orchestrator deletes, so a ticket run adding a file never conflicts with it
        for old in sorted(folder.glob("*.json"))[:-KEEP]:
            old.unlink()
    ctx.store.save("orchestrator: " + (", ".join(f"#{d['ticket']} {d['action']}" for d in decisions) or "nothing to do"))
