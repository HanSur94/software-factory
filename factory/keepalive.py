"""Keep the orchestrator going when GitHub drops its scheduled runs.

The orchestrator (orchestrate.yml) is started by a cron schedule in the product's factory.yml.
GitHub runs schedules on a best-effort basis and drops many of them: on one busy project,
a schedule of every 30 minutes ran three times in one day. Waiting tickets then waited for
hours with free slots.

The board runs on the owner's machine all day, so it steps in: when a connected project's
orchestrator has not run for longer than board.keepalive_minutes, the board starts it with
workflow_dispatch (input orchestrate=true). A product workflow without that input answers
"Unexpected inputs"; such a project is skipped, its other runs are not touched.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime

WORKFLOW = "factory.yml"


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], text=True, capture_output=True, timeout=60)


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def last_orchestration(repo: str) -> float:
    """When the orchestrator last started: the latest scheduled run of the factory workflow."""
    proc = _gh("api", f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?event=schedule&per_page=1")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-200:])
    runs = json.loads(proc.stdout).get("workflow_runs") or []
    return _epoch(runs[0]["created_at"]) if runs else 0.0


def kick(repo: str) -> str:
    """Start the orchestrator of this project. 'ok', 'unsupported' (old workflow) or the error."""
    proc = _gh("workflow", "run", WORKFLOW, "--repo", repo, "-f", "orchestrate=true")
    if proc.returncode == 0:
        return "ok"
    return "unsupported" if "nexpected input" in proc.stderr else proc.stderr.strip()[-200:]


def tick(repos: list[str], state: dict, now: float, minutes: float, *, last=last_orchestration, start=kick) -> list[str]:
    """Start the orchestrator of every project whose last run, scheduled or started here, is too old."""
    started = []
    for repo in repos:
        if state.get(repo, {}).get("unsupported") or state.get(repo, {}).get("paused"):
            continue   # an old workflow, or a project stopped on the board: nothing to start
        seen = state.setdefault(repo, {"kicked": 0.0})
        try:
            ran = max(last(repo), seen["kicked"])
        except Exception as exc:
            print(f"[factory] keepalive {repo}: {exc}", flush=True)
            continue
        if now - ran < minutes * 60:
            continue
        result = start(repo)
        if result == "ok":
            seen["kicked"] = now
            started.append(repo)
            print(f"[factory] {repo}: Orchestrator angestoßen (GitHub hat den Zeitplan ausgelassen)", flush=True)
        elif result == "unsupported":
            seen["unsupported"] = True
            print(f"[factory] {repo}: factory.yml kennt orchestrate=true nicht, kein Ersatz für den Zeitplan", flush=True)
        elif "disabled workflow" in result:
            seen["paused"] = True   # switched off on the board or on GitHub: wait until it is switched on again
            print(f"[factory] {repo}: Factory-Workflow ist ausgeschaltet, der Orchestrator wird nicht angestoßen", flush=True)
        else:
            print(f"[factory] keepalive {repo}: {result}", flush=True)
    return started


# What the board did per project: when it last started the orchestrator. The board page shows it.
STATE: dict = {}
MINUTES = 40.0   # board.keepalive_minutes, set when the board starts


def start(repos: list[str], conf: dict) -> None:
    """Check every few minutes for as long as the board runs. board.keepalive_minutes: 0 turns it off."""
    global MINUTES
    minutes = MINUTES = float(conf.get("keepalive_minutes", 40))
    if not minutes or not repos:
        return
    def loop():
        while True:
            try:
                tick(repos, STATE, time.time(), minutes)
            except Exception as exc:   # a failed look must not end the loop
                print(f"[factory] keepalive: {exc}", flush=True)
            time.sleep(300)

    threading.Thread(target=loop, name="factory-keepalive", daemon=True).start()
