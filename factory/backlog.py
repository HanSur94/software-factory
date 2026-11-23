"""The backlog planner: proposes the next tickets when the factory has nothing to do.

Runners that stand still are throughput lost. When no ticket of the project is started,
queued or running and no earlier proposal is still unanswered, the orchestrator's tick runs
one agent (role backlog) over the project's design documents and its issues. At most one round
per backlog.every_hours.

backlog.mode says what happens to a proposal:
- manual (default): it becomes an issue labelled `proposed` and shows on the board under "Braucht dich".
  Nothing is built until the owner starts it there (or sets factory:go); dropping it closes the issue.
- auto: it becomes an issue and starts right away, like a ticket the owner started.

Off unless the project sets backlog.enabled in its profile overrides.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import contracts, stations

BUSY = ("go", "queued", "running", "proposed")
KEEP_RUNS = 10   # sessions of the planner kept on the state branch


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state_file(ctx) -> Path:
    return Path(ctx.store.tickets_dir()).parent / "backlog.json"


def _issues(host, state: str, since: str | None = None) -> list[dict]:
    query = f"issues?state={state}&per_page=100&sort=updated&direction=desc" + (f"&since={since}" if since else "")
    found = host._api("GET", query, check=False) or []
    return [{"number": i["number"], "title": i["title"], "labels": [label["name"] for label in i.get("labels", [])]}
            for i in found if "pull_request" not in i]


def _body(p: dict, auto: bool = False) -> str:
    how = ("Vorgeschlagen vom Backlog-Planer der Software Factory und automatisch gestartet (backlog.mode: auto)."
           if auto else "Vorgeschlagen vom Backlog-Planer der Software Factory. Auf dem Board startest oder verwirfst "
           "du den Vorschlag; auf GitHub startet ihn das Label `factory:go`, Schließen verwirft ihn.")
    return "\n".join([
        p["mandate"], "", "**Abnahmekriterien**", *[f"- {a}" for a in p["acceptance"]],
        *(["", "**Wo:** " + ", ".join(f"`{w}`" for w in p["where"])] if p.get("where") else []),
        "", f"**Warum jetzt:** {p['why']}", "", how,
    ])


def tick(ctx, host) -> str:
    """One look: propose tickets if the factory is idle and it is time. Returns what happened."""
    cfg = ctx.cfg.get("backlog") or {}
    if not cfg.get("enabled"):
        return "off"
    if any(host.open_issues(ctx.label(name)) for name in BUSY):
        return "busy"
    path = _state_file(ctx)
    state = json.loads(path.read_text()) if path.exists() else {}
    last = state.get("last")
    if last and _now() - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) \
            < timedelta(hours=float(cfg.get("every_hours", 24))):
        return "recent"

    work = Path(ctx.workdir) / "backlog"
    work.mkdir(parents=True, exist_ok=True)
    open_file, closed_file, out = work / "open-issues.json", work / "closed-issues.json", work / "backlog.json"
    open_file.write_text(json.dumps(_issues(host, "open"), indent=1, ensure_ascii=False))
    since = (_now() - timedelta(days=int(cfg.get("closed_days", 30)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    closed_file.write_text(json.dumps(_issues(host, "closed", since), indent=1, ensure_ascii=False))
    sources = cfg.get("sources") or ["README.md", "docs/"]
    values = {"sources": ", ".join(f"`{s}`" for s in sources), "open_issues": open_file,
              "closed_issues": closed_file, "max": int(cfg.get("max_proposals", 3)), "out": out}
    # The session goes where the board can read it: one folder per run on the state branch, like a ticket's.
    runs = path.parent / "orchestrator" / "backlog"
    run = runs / _now().strftime("%Y%m%dT%H%M%SZ")
    run.mkdir(parents=True, exist_ok=True)
    saved_dir, saved_log = getattr(ctx, "run_dir", None), getattr(ctx, "log_dir", None)
    ctx.run_dir, ctx.log_dir = run, None
    try:
        _, result = stations.run_station(ctx, "backlog", ctx.cfg["roles"]["backlog"]["prompt"], values, Path(ctx.repo), out,
                                         lambda p: contracts.read_json(p, "backlog"), readonly=True)
    finally:
        ctx.run_dir, ctx.log_dir = saved_dir, saved_log
    proposals = result["proposals"][: int(cfg.get("max_proposals", 3))]
    auto = cfg.get("mode", "manual") == "auto"
    created = [host.create_issue(p["title"], _body(p, auto), [] if auto else [ctx.label("proposed")]) for p in proposals]
    if auto:   # a label set by the factory's own token starts no workflow: start each ticket directly
        for number in created:
            host.dispatch(number)
    (run / "proposals.json").write_text(json.dumps(
        [{"issue": n, "title": p["title"]} for n, p in zip(created, proposals)], indent=1, ensure_ascii=False) + "\n")
    for old in sorted(d for d in runs.iterdir() if d.is_dir())[:-KEEP_RUNS]:
        shutil.rmtree(old)
    state.update(last=_now().strftime("%Y-%m-%dT%H:%M:%SZ"), created=created)
    path.write_text(json.dumps(state, indent=2) + "\n")
    ctx.store.save(f"backlog: {len(created)} proposals")
    return f"{'started' if auto else 'proposed'} {','.join(str(n) for n in created) or 'nothing'}"
