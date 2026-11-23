"""A small local web page: every ticket as a card on a Kanban board.

Reads the same state the factory writes (status.json, ticket.json, verdict.json,
events.jsonl), either from the factory-state branch on GitHub or from a local
state folder. The board only reads; nothing here changes a ticket. The settings page next to it
(/settings) is the one place that writes: settings.yaml and a project's overrides.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import yaml

from . import cleanup, config, connect, floormap, flowlog, keepalive, machine, overview, settings, usage, watch
from . import judge as judging
from .config import ROOT

COLUMNS = [
    ("proposed", "Vorschlag"), ("queue", "Eingang"), ("plan", "Planung"), ("build", "Umsetzung"), ("verify", "Prüfung"),
    ("waiting", "Wartet auf dich"), ("stopped", "Angehalten"), ("review", "Pull Request"), ("done", "Fertig"),
]
PHASES = {
    "intake": ("queue", "Eingang"), "plan": ("plan", "Planer"), "plan_check": ("plan", "Plan-Prüfung"),
    "implement": ("build", "Implementierer"), "rework_judge": ("build", "Nacharbeit nach dem Richter"),
    "rework_ci": ("build", "Nacharbeit nach der CI"),
    "rework_base": ("build", "löst den Merge-Konflikt mit der Basis"),
    "checks": ("verify", "Checks und Review"), "judge": ("verify", "Richter"),
    "delivered": ("review", "wartet auf deinen Merge"), "merged": ("done", "automatisch gemergt"),
    "rejected": ("done", "abgelehnt"), "split": ("done", "aufgeteilt in Teil-Tickets"),
}
WAITING = {"intake_questions": "Rückfrage zum Issue", "plan_questions": "Rückfrage des Planers",
           "plan_approval": "Plan wartet auf /approve"}

QUERY = """
query($owner: String!, $name: String!, $tree: String!, $sweeps: String!, $backlog: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: $tree) {
      ... on Tree { entries { name object { ... on Tree { entries { name object { ... on Blob { text } } } } } } }
    }
    sweeps: object(expression: $sweeps) { ... on Tree { entries { name object { ... on Blob { text } } } } }
    backlog: object(expression: $backlog) { ... on Tree { entries { name } } }
    issues(first: 50, states: OPEN, labels: ["factory:go", "factory:running", "factory:waiting", "factory:needs-human", "factory:review", "factory:queued", "factory:part", "factory:proposed"]) {
      nodes { number title url labels(first: 10) { nodes { name } } }
    }
  }
}
"""


def _json(text: str | None):
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return None


# The stations of one ticket, in order, with the event names that belong to each.
STATIONS = [
    ("intake", "Eingang", ("intake",)), ("planner", "Planer", ("planner",)),
    ("plan_check", "Plan-Prüfung", ("plan_check",)), ("implementer", "Implementierer", ("implementer", "rework")),
    ("checks", "Checks", ("checks", "triage")), ("review", "Review", ("reviewer", "review")),
    ("judge", "Richter", ("judge",)), ("deliver", "Auslieferung", ("pipeline",)),
]
PHASE_INDEX = {"intake": 0, "plan": 1, "plan_check": 2, "implement": 3, "rework_judge": 3, "rework_ci": 3, "rework_base": 3, "checks": 4, "judge": 6,
               "delivered": 8, "merged": 8, "rejected": 0, "split": 3}
CURRENT_INDEX = {"intake": 0, "planner": 1, "plan_check": 2, "implementer": 3, "rework": 3, "checks": 4, "triage": 4,
                 "reviewer": 5, "judge": 6}


def _front_matter(text: str | None) -> dict:
    if not text or not text.startswith("---"):
        return {}
    try:
        return yaml.safe_load(text.split("---", 2)[1]) or {}
    except (yaml.YAMLError, IndexError):
        return {}


def _model(name: str | None) -> str:
    if not name or name == "fake":
        return "Trockenlauf" if name else ""
    parts = name.removeprefix("claude-").split("-")
    return f"{parts[0].capitalize()} {'.'.join(parts[1:])}".strip()


def _where(runtime: dict | None) -> str:
    """Where a station ran, in words."""
    if not runtime:
        return ""
    box = "in einem Container" if runtime.get("container") else "kein Docker"
    if runtime.get("host") == "github-hosted":
        return f"GitHub-Runner, frische VM ({runtime.get('os', '')}), {box}, Job {runtime.get('job', '')}"
    if runtime.get("host") == "self-hosted":
        return f"eigener Runner {runtime.get('runner', '')} ({runtime.get('os', '')}), {box}, Job {runtime.get('job', '')}"
    return f"dieser Rechner ({runtime.get('os', '')}), {box}, ohne Sandbox"


def workflow(status: dict, ticket: dict, verdict: dict | None, events: list[dict], files: dict, finished: str | None) -> dict:
    """One ticket as a list of stations: what is done, what runs now, what is still to come."""
    plan = _front_matter(files.get("plan.md"))
    review = _front_matter(files.get("review.md"))
    tasks = plan.get("tasks") or []

    def last(names, kind="verdict"):
        found = [e for e in events if e["station"] in names and e["kind"] == kind]
        return found[-1] if found else None

    phase = status.get("phase", "intake")
    active = CURRENT_INDEX.get(status.get("current"), PHASE_INDEX.get(phase, 0))
    if finished or phase in ("delivered", "merged"):
        active = 8
    summaries = {}
    if ticket:
        summaries["intake"] = f"Größe {ticket.get('size', '?')}" + (", sensibel" if ticket.get("sensitive") else "")
    if tasks:
        summaries["planner"] = f"{len(tasks)} Task" + ("s" if len(tasks) != 1 else "")
        done = len(status.get("done_tasks") or [])
        rework = status.get("rework_rounds", 0)
        summaries["implementer"] = f"{done} von {len(tasks)} Tasks" + (f", {rework} Nacharbeit" if rework else "")
        at_once = [w["task"] for w in status.get("workers") or []]
        if at_once:
            summaries["implementer"] += f" · {', '.join(at_once)} laufen gleichzeitig, jeder in einem eigenen Ordner"
    e = last(("plan_check",))
    if e:
        summaries["plan_check"] = ("bestanden" if e["outcome"] == "pass" else "Überarbeitung verlangt") + f" ({e.get('detail', '')})"
    e = last(("checks",))
    if e:
        summaries["checks"] = "grün" if e["outcome"] == "pass" else f"rot: {e.get('detail', '')}"
    e = last(("review",))
    if e:
        summaries["review"] = ("freigegeben" if e["outcome"] == "approve" else "Änderungen verlangt") + f" ({e.get('detail', '')})"
    if verdict:
        summaries["judge"] = "kein Urteil" if verdict.get("passed") is None else (
            f"{verdict['scenarios_passed']} von {verdict['scenarios_total']} Szenarien, "
            + ("bestanden" if verdict["passed"] else "nicht bestanden"))
    held = last(("pipeline",), "pipeline")
    outcomes = [e for e in events if e["station"] == "pipeline" and e.get("outcome") in ("merged", "delivered", "merge_held")]
    if phase == "merged":
        summaries["deliver"] = "automatisch gemergt"
    elif phase == "delivered":
        reason = next((e.get("detail") for e in reversed(outcomes) if e["outcome"] == "merge_held"), None)
        summaries["deliver"] = "Pull Request offen" + (f"; nicht gemergt: {reason}" if reason else "")
    if finished:
        summaries["deliver"] = finished

    stations = []
    for index, (sid, title, names) in enumerate(STATIONS):
        # The judge's own run stays in its job; its verdict event carries cost, duration and skills instead.
        source = [e for e in events if e["station"] == "judge" and e["kind"] == "verdict" and e.get("model")] \
            if sid == "judge" else [e for e in events if e["station"] in names and e["kind"] == "agent"]
        runs = [{"model": _model(e.get("model")), "effort": e.get("effort", ""), "cost": e.get("cost_usd", 0.0),
                 "seconds": e.get("duration_s", 0.0), "ok": e.get("outcome") != "error", "round": e.get("round", 1),
                 "label": "Nacharbeit" if e["station"] == "rework" else ("Triage" if e["station"] == "triage" else ""),
                 "task": e.get("task", ""),
                 # None = this run was made before skills were recorded
                 "skills_used": e.get("skills_used"), "skills_offered": e.get("skills_offered") or [],
                 "turns": e.get("turns", 0), "subagents": e.get("subagents", 0),
                 "tools": e.get("tools") or {}, "where": _where(e.get("runtime")), "runner": (e.get("runtime") or {}).get("runner") or "",
                 "tokens": e.get("tokens") or None, "context_peak": e.get("context_peak") or 0,
                 "context_window": e.get("context_window") or 0, "subscription": e.get("subscription") or {},
                 "rtk": e.get("rtk") or None, "ts": e["ts"]}
                for e in source]
        seconds = sum(r["seconds"] for r in runs)
        if sid == "checks":
            seconds += sum(e.get("duration_s", 0.0) for e in events if e["station"] == "checks" and e["kind"] == "check")
        touched = any(e["station"] in names for e in events)
        if phase == "split" and index >= active:
            state = "skipped"   # the parts build it, each with its own run through these stations
        elif index < active:
            state = "done" if (touched or sid in ("implementer", "checks", "deliver")) else "skipped"
        elif index == active:
            state = "failed" if status.get("stopped") or phase == "rejected" else ("waiting" if status.get("waiting") else "active")
        else:
            state = "pending"
        stations.append({"id": sid, "title": title, "state": state, "summary": summaries.get(sid, ""), "runs": runs,
                         "seconds": round(seconds, 1), "cost": round(sum(r["cost"] for r in runs), 4)})

    wall = 0.0
    if len(events) > 1:
        stamp = lambda e: datetime.strptime(e["ts"], "%Y-%m-%dT%H:%M:%SZ")
        wall = (stamp(events[-1]) - stamp(events[0])).total_seconds()
    used = [k for r in (r for st in stations for r in st["runs"]) for k in (r["skills_used"] or [])]
    tools: dict = {}
    for st in stations:
        for r in st["runs"]:
            for name, count in r["tools"].items():
                tools[name] = tools.get(name, 0) + count
    runs_in_time = sorted((r for st in stations for r in st["runs"]), key=lambda r: r["ts"])
    tokens: dict = {}
    for r in runs_in_time:
        for name, count in (r["tokens"] or {}).items():
            tokens[name] = tokens.get(name, 0) + count
    # Share of the Claude plan's limit: where it stood when the ticket's first run began and its last run ended.
    plan: dict = {}
    for r in runs_in_time:
        for window, seen in r["subscription"].items():
            entry = plan.setdefault(window, {"start": seen.get("start"), "models": []})
            entry.update(end=seen.get("end"), resets_at=seen.get("resets_at"), ts=r["ts"])
            if r["model"] and r["model"] not in entry["models"]:
                entry["models"].append(r["model"])   # Claude reports some windows only for some models
    return {
        "stations": stations,
        "totals": {"tokens": tokens, "subscription": plan,
                   "work_seconds": round(sum(st["seconds"] for st in stations), 1), "wall_seconds": wall,
                   "cost": round(sum(st["cost"] for st in stations), 4), "skills_used": sorted(set(used)), "tools": tools,
                   "skills_recorded": any(r["skills_used"] is not None for st in stations for r in st["runs"])},
        "mandate": ticket.get("mandate", ""), "acceptance": ticket.get("acceptance") or [],
        "tasks": [{"id": t.get("id", ""), "title": t.get("title", ""), "done": t.get("id") in (status.get("done_tasks") or [])}
                  for t in tasks],
        "review": {"blocking": [f.get("text", "") for f in review.get("blocking") or []],
                   "minor": [f.get("text", "") for f in review.get("minor") or []]} if review else None,
        "failures": [f["symptom"] for f in (verdict or {}).get("failures", [])],
        "notes": status.get("notes") or [], "run_urls": status.get("run_urls") or [],
        "timeline": [{"ts": e["ts"], "station": e["station"], "kind": e["kind"], "outcome": e.get("outcome", ""),
                      "detail": e.get("detail", "")} for e in events][-80:],
        # What the agents did, tool call by tool call. The judge's calls are not here: they stay in its job.
        "activity": [a for a in (_json(line) for line in (files.get("activity.jsonl") or "").splitlines()) if a][-150:],
    }


TICKET_QUERY = """
query($owner: String!, $name: String!, $tree: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: $tree) {
      ... on Tree { entries { name object {
        ... on Blob { text }
        ... on Tree { entries { name object { ... on Blob { text } } } }
      } } }
    }
  }
}
"""
# Files a station leaves behind, shown next to its sessions.
OUTPUTS = {"intake": ("ticket.json",), "planner": ("plan.md",), "plan_check": ("plan-review.json",),
           "implementer": ("notes-",), "checks": ("triage.md",), "review": ("review.md",), "judge": ("verdict.json",)}
NOTES = {
    "judge": "Der Richter prüft mit Szenarien, die der Implementierer nie sieht. Du siehst hier alles: live, solange "
             "er läuft, und jede Runde danach. Die Sitzung liegt nur auf diesem Rechner, nie auf GitHub.",
    "deliver": "Die Auslieferung ist ein Skript und kein Agent, deshalb gibt es hier keine Sitzung.",
    "checks": "Die Kommandos aus dem Profil des Projekts, ausgeführt von der Factory im Ticket. Eine Agent-Sitzung "
              "gibt es hier nur, wenn ein Check fehlschlägt und ein Agent die Logs zusammenfasst.",
}


def github_ticket_files(repo: str, branch: str, number: int) -> tuple[dict, dict]:
    return github_state_files(repo, branch, f"tickets/{number}")


def github_state_files(repo: str, branch: str, folder: str) -> tuple[dict, dict]:
    """The files of one folder on the state branch, and those of its subfolders."""
    owner, name = repo.split("/", 1)
    proc = subprocess.run(["gh", "api", "graphql", "-f", f"query={TICKET_QUERY}", "-F", f"owner={owner}",
                           "-F", f"name={name}", "-F", f"tree={branch}:{folder}"],
                          text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-300:] or "gh api failed")
    files, dirs = {}, {}
    tree = json.loads(proc.stdout)["data"]["repository"].get("object") or {}
    for entry in tree.get("entries") or []:
        obj = entry.get("object") or {}
        if "entries" in obj:
            dirs[entry["name"]] = {f["name"]: (f.get("object") or {}).get("text") or "" for f in obj["entries"]}
        else:
            files[entry["name"]] = obj.get("text") or ""
    return files, dirs


def local_ticket_files(state_dir: Path, number: int) -> tuple[dict, dict]:
    return read_folder(state_dir / "tickets" / str(number))


def read_folder(root: Path) -> tuple[dict, dict]:
    """A folder's files, and the files of each folder in it, as text."""
    files, dirs = {}, {}
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        if entry.is_file():
            files[entry.name] = entry.read_text(errors="replace")
        elif entry.is_dir():
            dirs[entry.name] = {f.name: f.read_text(errors="replace") for f in sorted(entry.iterdir()) if f.is_file()}
    return files, dirs


def _parse_log(text: str | None) -> tuple[dict, str]:
    """logs/NN-station.md: a few 'key: value' lines, a blank line, then the agent's final answer."""
    if not text:
        return {}, ""
    head, _, final = text.partition("\n\n")
    meta = dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)
    return {"model": _model(meta.get("model")), "cost": float(meta.get("cost_usd") or 0),
            "seconds": float(meta.get("duration_s") or 0), "state": meta.get("state", "")}, final.strip()


def sessions(files: dict, dirs: dict) -> dict:
    """Everything that happened in the agent sessions of one ticket, grouped by station."""
    out = {}
    for sid, title, names in STATIONS:
        runs = [] if sid == "deliver" else agent_runs(files, dirs, names)
        prefixes = OUTPUTS.get(sid, ())
        outputs = [{"name": n, "text": t[:20000]} for n, t in sorted(files.items())
                   if any(n == pre or (pre.endswith("-") and n.startswith(pre)) for pre in prefixes)]
        live = []
        if sid == "checks":
            logs_of = dirs.get("checks") or {}
            state = _json(logs_of.get("state.json")) or {}
            # Each command with what it printed so far; a log is the newest output, so the end is what matters.
            live = [{**c, "log": (logs_of.get(f"{c['name']}.log") or "")[-20000:]} for c in state.get("checks") or []]
            if not live:   # a ticket from before state.json: just its logs
                live = [{"name": n[:-4], "run": "", "state": "", "log": t[-20000:]} for n, t in sorted(logs_of.items()) if n.endswith(".log")]
        out[sid] = {"title": title, "runs": runs, "outputs": outputs, "note": NOTES.get(sid, ""), "checks": live}
    return out


def agent_runs(files: dict, dirs: dict, names: tuple) -> list[dict]:
    """The recorded agent sessions in a state folder whose station is one of names, in the order they ran."""
    logs, recorded = dirs.get("logs") or {}, dirs.get("sessions") or {}
    agent_events = [e for e in (_json(line) for line in (files.get("events.jsonl") or "").splitlines())
                    if e and e["kind"] == "agent"]
    runs = []
    for stem in sorted({name.rsplit(".", 1)[0] for name in (*logs, *recorded)}):
        seq, _, name = stem.partition("-")
        if name not in names or not seq.isdigit():
            continue
        meta, final = _parse_log(logs.get(stem + ".md"))
        entries = [e for e in (_json(line) for line in (recorded.get(stem + ".jsonl") or "").splitlines()) if e]
        label = "Nacharbeit" if name == "rework" else ("Triage der Check-Logs" if name == "triage" else "")
        # Runs that worked at the same time finish in another order than they started, so newer events
        # carry the number of their run; older ones are matched by their place in the log.
        event = next((e for e in agent_events if e.get("seq") == int(seq)), None)
        if event is None:
            event = agent_events[int(seq) - 1] if int(seq) <= len(agent_events) else {}
        if event.get("station") == name:   # the log file and the event describe the same run
            meta.update(tokens=event.get("tokens"), context_peak=event.get("context_peak") or 0,
                        context_window=event.get("context_window") or 0, turns=event.get("turns") or 0,
                        effort=event.get("effort", ""), skills_used=event.get("skills_used"), task=event.get("task", ""),
                        subscription=event.get("subscription") or {})
        runs.append({"seq": int(seq), "station": name, "label": label, "meta": meta, "final": final, "entries": entries})
    return runs


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


def _live_round(pointer: dict) -> tuple[dict, dict, list[dict]] | None:
    """A judge at work on this machine: its private folder, read as it is written."""
    private = Path(str(pointer.get("private") or ""))
    if not private.is_dir():
        return None
    files, dirs = read_folder(private)
    scenarios = []
    for n, entry in enumerate(_json(files.get("manifest.json")) or [], 1):
        try:
            text = Path(entry["path"]).read_text(errors="replace")
        except (OSError, KeyError, TypeError):
            continue
        scenarios.append({"name": f"{n:02d}-{entry.get('id', 'S')}.md", "text": text[:20000]})
    return files, dirs, scenarios


def judge_session(repo: str, number: int) -> dict:
    """The judge's runs of a ticket on this machine, newest first: kept rounds (judge.keep_session), and a round
    that is running right now (judge.open_round), read live from the judge's private folder."""
    root = judging.archive_dir(repo, number)
    rounds = []
    for folder in sorted((d for d in root.iterdir() if d.is_dir()), reverse=True) if root.is_dir() else []:
        files, dirs = read_folder(folder)
        pointer = _json(files.get(judging.LIVE))
        state = "done"
        scenarios = [{"name": n, "text": t[:20000]} for n, t in sorted((dirs.get("scenarios") or {}).items())]
        if pointer is not None:
            live = _live_round(pointer) if _alive(pointer.get("pid")) else None
            if live is None:
                state = "broken"   # the judge ended without keeping its round (a cancelled run)
            else:
                state = "live"
                files, dirs, scenarios = live
        rounds.append({"at": folder.name, "state": state, "live": state == "live",
                       "runs": agent_runs(files, dirs, ("scenario_writer", "judge")), "scenarios": scenarios,
                       "result": _json(files.get("judge-result.json")), "verdict": _json(files.get("verdict.json"))})
    return {"rounds": rounds}


def backlog_session(files: dict, dirs: dict) -> dict:
    """One run of the backlog planner: its session like a station's, and the issues it proposed."""
    return {"runs": agent_runs(files, dirs, ("backlog",)), "proposals": _json(files.get("proposals.json")) or []}


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def evidence_of(text: str | None) -> dict | None:
    """The pictures of a ticket's last green round (factory/evidence.py): what the board may serve and show."""
    record = _json(text)
    if not isinstance(record, dict):
        return None
    items = [{k: i.get(k) for k in ("file", "title", "source", "side", "pair", "path", "hash")}
             for i in record.get("items") or []
             if isinstance(i, dict) and re.fullmatch(r"[\w.-]+", str(i.get("file") or ""))
             and Path(str(i["file"])).suffix.lower() in IMAGE_TYPES]
    return {"round": record.get("round"), "ts": record.get("ts"), "items": items,
            "skipped": record.get("skipped") or 0} if items else None


def evidence_bytes(repo: str, branch: str, number: int, name: str) -> bytes:
    proc = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                           f"repos/{repo}/contents/tickets/{number}/evidence/{name}?ref={branch}"],
                          capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip()[-300:] or "gh api failed")
    return proc.stdout


def floorplan_of(text: str | None) -> dict | None:
    """Where a ticket's change lies on the project's floor plan (factory/floormap.py): the parts it touched."""
    record = _json(text)
    if not isinstance(record, dict) or not record.get("parts"):
        return None
    keep = ("path", "status", "add", "del", "name", "d")
    return {"parts": [{"id": p.get("id"), "label": p.get("label"), "path": p.get("path") or [],
                       "files": [{k: f.get(k) for k in keep} for f in p.get("files") or []]}
                      for p in record["parts"] if isinstance(p, dict)],
            "more_parts": record.get("more_parts") or 0,
            "off_map": [{"path": c.get("path"), "status": c.get("status")} for c in record.get("off_map") or []]}


def state_raw(repo: str, branch: str, path: str) -> bytes:
    """One file of the state branch, whole (the GraphQL text of a big file is cut)."""
    proc = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{path}?ref={branch}"],
                          capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise FileNotFoundError(proc.stderr.decode(errors="replace").strip()[-300:] or path)
    return proc.stdout


class Maps:
    """The board's floor plans: the base branch's map of a project, and one with a ticket's files marked.

    Drawn here by floorplan's own build from the bundle on the state branch (factory/floormap.py), and kept until the
    map changes. Asking whether it changed costs one small read every couple of minutes."""

    def __init__(self, branch: str, workdir: Path, cfg_of):
        self.branch, self.workdir, self.cfg_of = branch, workdir, cfg_of
        self.meta: dict = {}      # repo -> (time read, meta)
        self.bundles: dict = {}   # repo -> (key, bundle)
        self.pages: dict = {}     # (repo, key, ticket) -> html
        self.lock = threading.Lock()

    def _meta(self, repo: str) -> dict:
        at, meta = self.meta.get(repo, (0.0, None))
        if meta is None or time.time() - at > 120:
            try:
                meta = json.loads(state_raw(repo, self.branch, floormap.META))
            except (FileNotFoundError, ValueError, subprocess.SubprocessError):
                meta = {}
            self.meta[repo] = (time.time(), meta)
        return meta

    def page(self, repo: str, ticket: int | None = None) -> bytes:
        meta = self._meta(repo)
        if not meta.get("head"):
            raise FileNotFoundError("Für dieses Projekt gibt es noch keine Karte. Der Orchestrator zeichnet sie bei "
                                    "seinem nächsten Lauf" + (f" (letzter Versuch: {meta['error']})" if meta.get("error") else "") + ".")
        key = f"{meta['head']}:{meta.get('built')}"
        with self.lock:
            done = self.pages.get((repo, key, ticket))
            if done and (not ticket or time.time() - done[1] < 300):   # a ticket's record changes with each round
                return done[0]
            if self.bundles.get(repo, (None,))[0] != key:
                self.bundles[repo] = (key, json.loads(state_raw(repo, self.branch, floormap.BUNDLE)))
            bundle = self.bundles[repo][1]
            record = None
            if ticket:
                try:
                    record = json.loads(state_raw(repo, self.branch, f"tickets/{ticket}/{floormap.DELTA}"))
                except (FileNotFoundError, ValueError):
                    raise FileNotFoundError(f"Ticket #{ticket} hat noch keinen Grundriss: er entsteht nach grünen Checks.")
            skill = floormap.skill_dir(self.cfg_of(), self.workdir)
            if skill is None or not floormap.node():
                raise FileNotFoundError("Zum Zeichnen braucht das Board floorplan (plugins.floorplan) und Node 18+.")
            html = floormap.page(skill, bundle, record)
            self.pages = {k: v for k, v in self.pages.items() if k[0] != repo or k[1] == key}   # older maps go
            self.pages[(repo, key, ticket)] = (html, time.time())
            return html


def card(repo: str, number: int, files: dict, issue: dict | None, url_base: str | None) -> dict:
    status = _json(files.get("status.json")) or {}
    ticket = _json(files.get("ticket.json")) or {}
    verdict = _json(files.get("verdict.json"))
    events = [e for e in (_json(line) for line in (files.get("events.jsonl") or "").splitlines()) if e]
    labels = issue["labels"] if issue else []

    column, step = PHASES.get(status.get("phase", "intake"), ("queue", "Eingang"))
    detail = ""
    questions: list[str] = []
    human = [e["outcome"] for e in events if e["kind"] == "human" and e.get("outcome") in ("merged", "closed")]
    aborted = ABORTED in labels
    if aborted:   # the cancelled run may not have written it down: its station shows stopped, not running
        status = {**status, "stopped": status.get("stopped") or "Von Hand abgebrochen", "waiting": None, "current": None}
    if human:
        column, step = "done", "gemergt" if human[-1] == "merged" else "verworfen"
    elif aborted:
        column, step = "stopped", "abgebrochen"
        detail = "Von Hand abgebrochen. Neu starten setzt es dort fort, wo es stand."
    elif status.get("stopped") and not (status.get("phase") == "split" and "Phase `split`" in status["stopped"]):
        column, detail = "stopped", status["stopped"]
    elif status.get("waiting"):
        column, detail = "waiting", WAITING.get(status["waiting"]["reason"], status["waiting"]["reason"])
        # what the agent asks, word for word: from the intake's ticket or the planner's plan
        reason = status["waiting"]["reason"]
        asked = ticket.get("questions") if reason == "intake_questions" else \
            _front_matter(files.get("plan.md")).get("questions") if reason == "plan_questions" else None
        questions = [str(q) for q in asked or [] if str(q).strip()][:12]
    elif status.get("queued"):
        column, step, detail = "queue", "wartet auf einen freien Platz", status["queued"]
    elif not status and PROPOSED in labels:
        column, step = "proposed", "Vorschlag des Backlog-Planers"
        detail = "Wartet auf dich: Starten baut es, Verwerfen schließt das Issue."
    elif not status and "factory:blocked" in labels:
        step = "wartet auf die Teile, von denen es abhängt"
    elif not status and "factory:queued" in labels:
        step = "wartet auf einen freien Platz"
    splits = [e for e in events if e["kind"] == "pipeline" and e.get("outcome") == "split"]
    parts = [int(n) for n in re.findall(r"#(\d+)", splits[-1].get("detail") or "")] \
        if splits and status.get("phase") == "split" else None

    last = events[-1] if events else None
    flow = workflow(status, ticket, verdict, events, files,
                    ("gemergt" if human[-1] == "merged" else "verworfen") if human else None)
    log = flowlog.lanes(events, column, status.get("phase"), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return {
        "repo": repo, "number": number, "column": column, "step": step, "detail": detail[:220], "parts": parts,
        "questions": questions,
        "title": ticket.get("title") or (issue or {}).get("title") or f"Ticket {number}",
        "round": status.get("round", 1), "spent": status.get("spent_usd", 0.0),
        "running": not aborted and ("factory:running" in labels or (issue is None and bool(status.get("current")))),
        "aborted": aborted,
        "verdict": None if not verdict or verdict.get("passed") is None else {
            "passed": verdict["passed"], "text": f"{verdict['scenarios_passed']}/{verdict['scenarios_total']} Szenarien"},
        "last": {"station": last["station"], "ts": last["ts"]} if last else None,
        "started": events[0]["ts"] if events else None,
        "issue_url": (issue or {}).get("url") or (f"{url_base}/issues/{number}" if url_base else None),
        "pr_url": status.get("pr") if str(status.get("pr") or "").startswith("http") else None,
        "branch": status.get("branch"), "base": status.get("base_branch"), "worktree": status.get("worktree"),
        "workers": status.get("workers") or [],
        "evidence": evidence_of(files.get("evidence.json")),
        "floorplan": floorplan_of(files.get("floorplan.json")),
        "restartable": column in RESTARTABLE,
        # a merged branch is deleted, so only link it while it can still exist
        "branch_url": f"{url_base}/tree/{status['branch']}" if url_base and status.get("branch")
        and status.get("phase") != "merged" else None,
        "workflow": flow,
        # Where it spent its time and on which runner: the timetable and the signal box draw these.
        "lanes": log["lanes"], "end": log["end"], "bays": flowlog.bays(flow["stations"]),
    }


# ---- the project's own CI on a factory pull request ------------------------------

CI_FIELDS = """
  state merged mergeable baseRefName
  commits(last: 1) { nodes { commit { oid statusCheckRollup { state contexts(first: 50) { nodes {
    __typename
    ... on CheckRun { name status conclusion detailsUrl startedAt completedAt
                      checkSuite { workflowRun { workflow { name } } } }
    ... on StatusContext { context state targetUrl createdAt }
  } } } } } }
"""


def _seconds(start: str | None, end: str | None) -> float:
    if not start or not end:
        return 0.0
    stamp = lambda t: datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ")
    return max(0.0, (stamp(end) - stamp(start)).total_seconds())


def _check(node: dict) -> dict:
    """One check run or commit status in words the page can show."""
    if node.get("__typename") == "StatusContext":
        state = {"SUCCESS": "ok", "PENDING": "running", "EXPECTED": "waiting"}.get(node.get("state"), "failed")
        return {"name": node.get("context", ""), "workflow": "", "state": state, "url": node.get("targetUrl"), "seconds": 0.0,
                "conclusion": (node.get("state") or "").lower()}
    status, conclusion = node.get("status"), node.get("conclusion")
    if status != "COMPLETED":
        state = "running" if status == "IN_PROGRESS" else "waiting"
    else:
        state = {"SUCCESS": "ok", "NEUTRAL": "ok", "SKIPPED": "skipped", "CANCELLED": "cancelled"}.get(conclusion, "failed")
    workflow = ((((node.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow")) or {}).get("name", "")
    return {"name": node.get("name", ""), "workflow": workflow, "state": state, "url": node.get("detailsUrl"),
            "seconds": _seconds(node.get("startedAt"), node.get("completedAt")),
            "conclusion": (conclusion or status or "").lower()}


def ci_of_pull_requests(repo: str, numbers: list[int]) -> dict[int, dict]:
    """The checks GitHub ran on the newest commit of each pull request, in one query."""
    if not numbers:
        return {}
    owner, name = repo.split("/", 1)
    body = "\n".join(f"p{n}: pullRequest(number: {n}) {{ {CI_FIELDS} }}" for n in sorted(set(numbers)))
    query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {body} }} }}'
    proc = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"], text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-300:] or "gh api failed")
    data = json.loads(proc.stdout)["data"]["repository"]
    out = {}
    for n in set(numbers):
        pr = data.get(f"p{n}") or {}
        commit = (((pr.get("commits") or {}).get("nodes") or [{}])[0] or {}).get("commit") or {}
        rollup = commit.get("statusCheckRollup") or {}
        checks = [_check(c) for c in (rollup.get("contexts") or {}).get("nodes") or []]
        out[n] = {"state": (rollup.get("state") or "").lower() or None, "checks": checks, "commit": (commit.get("oid") or "")[:7],
                  "pr_state": pr.get("state"), "conflict": pr.get("mergeable") == "CONFLICTING",
                  "base": pr.get("baseRefName") or ""}
    return out


def ci_station(card: dict, ci: dict | None) -> dict:
    """The project's CI as the last station of a ticket: what GitHub ran on its pull request."""
    station = {"id": "ci", "title": "CI des Projekts", "runs": [], "cost": 0.0, "seconds": 0.0, "checks": [], "summary": ""}
    if card.get("parts"):
        return {**station, "state": "skipped", "summary": "jedes Teil-Ticket hat seinen eigenen Pull Request"}
    if not card.get("pr_url") and card.get("column") == "done":   # rejected
        return {**station, "state": "skipped", "summary": "kein eigener Pull Request"}
    if not card.get("pr_url"):
        return {**station, "state": "pending", "summary": "läuft, sobald der Pull Request offen ist"}
    checks = [c for c in (ci or {}).get("checks", []) if c["state"] != "skipped"]
    station["checks"], station["commit"] = (ci or {}).get("checks", []), (ci or {}).get("commit", "")
    station["seconds"] = round(sum(c["seconds"] for c in checks), 1)
    if ci is None:
        return {**station, "state": "pending", "summary": "Stand der CI nicht abrufbar"}
    if not checks:
        return {**station, "state": "skipped", "summary": "keine CI-Läufe auf diesem Pull Request"}
    count = lambda *states: sum(c["state"] in states for c in checks)
    ok, bad, busy = count("ok"), count("failed", "cancelled"), count("running", "waiting")
    parts = [f"{ok} von {len(checks)} grün"] + ([f"{bad} rot"] if bad else []) + ([f"{busy} laufen"] if busy else [])
    state = "failed" if bad else "active" if busy else "done"
    return {**station, "state": state, "summary": " · ".join(parts)}


def github_cards(repo: str, branch: str, extra: dict | None = None) -> list[dict]:
    """The cards of a project; with extra, also what its orchestrator recorded (extra["orchestrator"])."""
    owner, name = repo.split("/", 1)
    proc = subprocess.run(["gh", "api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"name={name}",
                           "-F", f"tree={branch}:tickets", "-F", f"sweeps={branch}:orchestrator/sweeps",
                           "-F", f"backlog={branch}:orchestrator/backlog"],
                          text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-300:] or "gh api failed")
    data = json.loads(proc.stdout)["data"]["repository"]
    if extra is not None:
        extra["orchestrator"] = orchestration(data.get("sweeps"))
        extra["orchestrator"]["backlog_runs"] = sorted((e["name"] for e in (data.get("backlog") or {}).get("entries") or []),
                                                       reverse=True)
    issues = {i["number"]: {"title": i["title"], "url": i["url"], "labels": [n["name"] for n in i["labels"]["nodes"]]}
              for i in data["issues"]["nodes"]}
    url_base = f"https://github.com/{repo}"
    cards, seen = [], set()
    for entry in ((data.get("object") or {}).get("entries") or []):
        if not entry["name"].isdigit():
            continue
        number = int(entry["name"])
        files = {f["name"]: (f.get("object") or {}).get("text") for f in (entry.get("object") or {}).get("entries") or []}
        cards.append(card(repo, number, files, issues.get(number), url_base))
        seen.add(number)
    for number, issue in issues.items():   # labelled, but the factory has not written state yet
        if number not in seen:
            cards.append(card(repo, number, {}, issue, url_base))
    prs = {c["number"]: int(m.group(1)) for c in cards
           if (m := re.match(re.escape(url_base) + r"/pull/(\d+)", c.get("pr_url") or ""))}
    try:
        ci = ci_of_pull_requests(repo, list(prs.values()))
    except Exception:   # the board still shows the tickets when the CI cannot be read
        ci = None
    follow_parts(cards)
    for c in cards:
        c["ci"] = None if ci is None else ci.get(prs.get(c["number"]))
        closed_pr(c, c["ci"])
        conflicted_pr(c, c["ci"])
        c["workflow"]["stations"].append(ci_station(c, c["ci"]))
    return cards


def closed_pr(card: dict, ci: dict | None) -> None:
    """A ticket waiting for a merge whose pull request is no longer open is done: GitHub knows best.

    The factory records a merge or close when it sees it (record-outcome), but it can miss one."""
    if card["column"] != "review" or not ci or ci.get("pr_state") not in ("MERGED", "CLOSED"):
        return
    card["column"] = "done"
    card["step"] = "gemergt" if ci["pr_state"] == "MERGED" else "Pull Request geschlossen, ohne Merge"
    card["restartable"] = False


def conflicted_pr(card: dict, ci: dict | None) -> None:
    """A pull request GitHub cannot merge because its base changed the same lines: the card says so first."""
    if card["column"] != "review" or not ci or not ci.get("conflict") or ci.get("pr_state") != "OPEN":
        return
    base = ci.get("base") or card.get("base") or "der Basis"
    card["conflict"] = True
    card["step"] = f"Merge-Konflikt mit {base}"
    note = (f"GitHub kann diesen Pull Request nicht mergen: {base} hat sich an denselben Stellen geändert. "
            "Erst den Konflikt lösen, dann mergen.")
    card["detail"] = note + (" " + card["detail"] if card.get("detail") else "")


def orchestration(tree: dict | None, keep: int = 40) -> dict:
    """The orchestrator's newest records, newest first."""
    entries = sorted((tree or {}).get("entries") or [], key=lambda e: e["name"], reverse=True)
    sweeps = [s for s in (_json((e.get("object") or {}).get("text")) for e in entries[:keep]) if s]
    return {"sweeps": sweeps}


def follow_parts(cards: list[dict]) -> None:
    """A split ticket is not done when it is split: it is built by its parts and done when they are."""
    by_number = {c["number"]: c for c in cards}
    for c in cards:
        if not c.get("parts") or c["column"] != "done":
            continue
        parts = [by_number.get(n) for n in c["parts"]]
        done = sum(1 for p in parts if p and p["column"] == "done")
        if done == len(parts):
            c["step"] = f"alle {len(parts)} Teile fertig"
            continue
        busy = [f"#{n}" for n, p in zip(c["parts"], parts) if p and p["running"]]
        c["column"], c["step"], c["restartable"] = "build", f"in Teil-Tickets: {done} von {len(parts)} fertig", False
        c["detail"] = ("läuft gerade: " + ", ".join(busy)) if busy else "Teile: " + ", ".join(f"#{n}" for n in c["parts"])


def local_cards(state_dir: Path) -> list[dict]:
    name = f"{state_dir.parent.name} (lokal)"
    cards = []
    for d in sorted((state_dir / "tickets").glob("*")):
        if d.is_dir() and d.name.isdigit():
            files = {f.name: f.read_text() for f in d.iterdir() if f.is_file() and f.suffix in (".json", ".jsonl", ".md")}
            cards.append(card(name, int(d.name), files, None, None))
    for c in cards:   # a local run has no pull request on GitHub, so no CI either
        c["ci"] = None
        c["workflow"]["stations"].append({**ci_station(c, None), "state": "skipped", "summary": "lokaler Lauf, keine CI"})
    return cards


def mark_restarts(cards: list[dict], pending: dict) -> None:
    """A restart takes about a minute before its run writes anything. Until then the board says that it was
    started, instead of still showing the ticket as stopped (and offering the button again)."""
    for c in cards:
        started = pending.get((c["source"], c["number"]))
        if not started or time.time() - started > 600:
            continue
        if (c["last"] or {}).get("ts", "") >= time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started)):
            continue   # the new run has written its first event: the card shows it from here on
        c.update(running=True, restartable=False, restarting=True,
                 detail="Neustart angestoßen: der Lauf startet auf GitHub, das dauert etwa eine Minute.")


def _show_cost() -> bool:
    """board.show_cost, read on every refresh: the settings page switches it without a restart."""
    try:
        return bool((config.load_config().get("board") or {}).get("show_cost"))
    except Exception:
        return False


TOKEN_FILE = Path.home() / ".cache" / "software-factory" / "board-token"


def _page_token() -> str:
    """The page's write token, kept across restarts: a tab opened before a restart can still act.

    Only this user can read the file, as only a page served from here can read the token.
    """
    try:
        token = TOKEN_FILE.read_text().strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.touch(mode=0o600)
        TOKEN_FILE.chmod(0o600)
        TOKEN_FILE.write_text(token)
    except OSError:
        pass   # the board still works, only a restart then needs a reload of open tabs
    return token


def board(repos: list[str], local: list[str], branch: str, pending: dict | None = None, min_free_gb: float = 15) -> dict:
    cards, errors = [], []
    orchestrators: dict = {}
    for repo in repos:
        try:
            extra: dict = {}
            cards += [{**c, "source": f"gh:{repo}"} for c in github_cards(repo, branch, extra)]
            orchestrators[repo] = extra.get("orchestrator")
        except Exception as exc:
            errors.append(f"{repo}: {exc}")
    for index, path in enumerate(local):
        cards += [{**c, "source": f"local:{index}"} for c in local_cards(Path(path).expanduser())]
    cards.sort(key=lambda c: (c["last"] or {}).get("ts", ""), reverse=True)
    mark_restarts(cards, pending or {})
    plan: dict = {}
    for c in cards:
        for window, seen in c["workflow"]["totals"]["subscription"].items():
            if seen.get("end") is not None and seen.get("ts", "") >= plan.get(window, {}).get("ts", ""):
                plan[window] = {"used": seen["end"], "resets_at": seen.get("resets_at"), "ts": seen["ts"],
                                "models": seen.get("models") or []}
    plan, plan_source = usage.merge(plan, usage.live(), time.time())
    # Every source is its own project with its own board under /p/...; connected but empty must still show.
    projects = [{"source": f"gh:{repo}", "repo": repo, "name": repo.split("/", 1)[1], "owner": repo.split("/", 1)[0],
                 "path": f"/p/{repo}", "url": f"https://github.com/{repo}"} for repo in repos]
    projects += [{"source": f"local:{index}", "repo": f"{Path(path).expanduser().parent.name} (lokal)",
                  "name": f"{Path(path).expanduser().parent.name} (lokal)", "owner": "lokaler Lauf", "path": f"/p/local/{index}", "url": None}
                 for index, path in enumerate(local)]
    for p in projects:
        if p["source"].startswith("gh:"):
            seen = keepalive.STATE.get(p["repo"]) or {}
            p["orchestrator"] = {**(orchestrators.get(p["repo"]) or {"sweeps": [], "backlog_runs": []}),
                                 "kicked": seen.get("kicked") or None, "can_kick": not seen.get("unsupported"),
                                 "every_minutes": keepalive.MINUTES}
        p["tickets"] = sum(c["source"] == p["source"] for c in cards)
        p["running"] = sum(c["source"] == p["source"] and c["running"] for c in cards)
    return {"columns": [{"id": cid, "title": title} for cid, title in COLUMNS], "cards": cards, "errors": errors,
            "projects": projects, "overview": overview.build(cards, projects),
            "subscription": plan, "subscription_source": plan_source, "show_cost": _show_cost(), "updated": time.strftime("%H:%M:%S"),
            "disk": {"free_gb": round(watch.free_disk_gb(), 1), "min_gb": min_free_gb}}


# ---- starting a ticket again -------------------------------------------------------

# Where a restart makes sense: not while the ticket waits for an answer, and not once it is delivered.
RESTARTABLE = ("queue", "plan", "build", "verify", "stopped")


def restart(repo: str, number: int) -> dict:
    """Start a ticket's workflow again, as the owner. The pipeline picks the ticket up where its state
    says it stands: a stopped ticket starts again at the station it stopped at, with a fresh budget;
    one whose run broke off (a runner that died, a cancelled run) carries on from its phase. A run of
    the same ticket that is still going is not doubled: the project workflow queues the new one behind it."""
    workflow = Path(connect.WORKFLOW).name
    # An aborted ticket only runs again once its label is gone (pipeline.work stops at it).
    _gh_ok("api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/{quote(ABORTED, safe='')}")
    base = subprocess.run(["gh", "repo", "view", repo, "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
                          text=True, capture_output=True, timeout=60)
    if base.returncode != 0 or not base.stdout.strip():
        raise RuntimeError(base.stderr.strip()[-300:] or f"{repo}: Standard-Branch nicht lesbar")
    proc = subprocess.run(["gh", "workflow", "run", workflow, "--repo", repo, "--ref", base.stdout.strip(),
                           "-f", f"issue={number}"], text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-300:] or "gh workflow run failed")
    return {"ok": True, "message": f"#{number} ist neu angestoßen; der Lauf erscheint gleich auf GitHub."}


def rework(repo: str, number: int) -> dict:
    """Send a delivered ticket whose judge was not satisfied back to the implementer, as the owner: the label
    `factory:rework` says so (pipeline.owner_rework), and the ticket's workflow is started to act on it."""
    if not _gh_ok("api", "-X", "POST", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={REWORK}"):
        raise RuntimeError(f"#{number}: Label {REWORK} ließ sich nicht setzen")
    restart(repo, number)
    return {"ok": True, "message": f"#{number} geht zum Nachbessern zurück; der Lauf erscheint gleich auf GitHub."}


def proposal(repo: str, number: int, action: str) -> dict:
    """The owner answers a proposal of the backlog planner: start it (the label factory:go, which starts the
    ticket's workflow as if they had set it on GitHub) or drop it (the issue is closed as not planned)."""
    if action == "start":
        if not _gh_ok("api", "-X", "POST", f"repos/{repo}/issues/{number}/labels", "-f", "labels[]=factory:go"):
            raise RuntimeError(f"#{number}: Label factory:go ließ sich nicht setzen")
        _gh_ok("api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/{quote(PROPOSED, safe='')}")
        return {"ok": True, "message": f"#{number} ist gestartet; der Lauf erscheint gleich auf GitHub."}
    if action == "drop":
        _gh_ok("api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f",
               "body=Vorschlag auf dem Board verworfen.")
        _gh_ok("api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/{quote(PROPOSED, safe='')}")
        if not _gh_ok("api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", "state=closed",
                      "-f", "state_reason=not_planned"):
            raise RuntimeError(f"#{number}: Issue ließ sich nicht schließen")
        return {"ok": True, "message": f"#{number} ist verworfen."}
    raise ValueError(f"unbekannte Aktion: {action}")


PROPOSED = "factory:proposed"
REWORK = "factory:rework"
ABORTED = "factory:aborted"
# Labels that start or resume a ticket, or say it works: an aborted ticket carries none of them.
ABORT_REMOVES = ("factory:go", "factory:running", "factory:queued", "factory:waiting")


def _gh_ok(*args: str) -> bool:
    return subprocess.run(["gh", *args], text=True, capture_output=True, timeout=60).returncode == 0


def _gh_json(*args: str):
    proc = subprocess.run(["gh", *args], text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-300:] or "gh failed")
    return json.loads(proc.stdout or "null")


def runs_of(number: int, title: str, run_urls: list[str], active: list[dict]) -> list[str]:
    """The unfinished runs that work on this ticket: those its state names, and those its issue started
    (a label or a comment: GitHub names such a run after the issue). A run started by hand or by the
    orchestrator names no ticket; it stops by itself at the label, at its start or its next phase."""
    mine = {u.rstrip("/").rsplit("/", 1)[-1] for u in run_urls}
    return [str(r["databaseId"]) for r in active
            if r.get("status") != "completed" and (str(r["databaseId"]) in mine or
                                                   (r.get("event") in ("issues", "issue_comment") and r.get("displayTitle") == title))]


def abort(repo: str, number: int, branch: str, force_after: float = 45.0) -> dict:
    """Stop a ticket now, in whatever phase: the label first, so nothing starts it again and a run that is not
    reached stops at its next phase; then every run of it is cancelled. Branch, pull request and state stay;
    the restart button carries on from there."""
    _gh_ok("label", "create", ABORTED, "--repo", repo, "--color", "B60205", "--force",
           "--description", "Von Hand abgebrochen: die Factory fasst das Ticket nicht an")
    _gh_json("api", "-X", "POST", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={ABORTED}")
    for name in ABORT_REMOVES:
        _gh_ok("api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/{quote(name, safe='')}")
    issue = _gh_json("api", f"repos/{repo}/issues/{number}")
    try:
        status = json.loads(subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                                            f"repos/{repo}/contents/tickets/{number}/status.json?ref={branch}"],
                                           text=True, capture_output=True, timeout=60).stdout or "{}")
    except ValueError:
        status = {}
    active = _gh_json("run", "list", "--repo", repo, "--workflow", Path(connect.WORKFLOW).name, "--limit", "100",
                      "--json", "databaseId,status,event,displayTitle") or []
    ids = runs_of(number, issue.get("title") or "", status.get("run_urls") or [], active)
    cancelled = [i for i in ids if _gh_ok("run", "cancel", i, "--repo", repo)]

    def insist():   # a run that does not end on a cancel (a step that ignores the signal) is ended by force
        time.sleep(force_after)
        for i in cancelled:
            run = _gh_json("api", f"repos/{repo}/actions/runs/{i}")
            if (run or {}).get("status") != "completed":
                _gh_ok("api", "-X", "POST", f"repos/{repo}/actions/runs/{i}/force-cancel")
    if cancelled:
        threading.Thread(target=insist, daemon=True).start()
    runs = ", ".join(f"[{i}](https://github.com/{repo}/actions/runs/{i})" for i in cancelled) or "keiner lief"
    _gh_ok("api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f",
           f"body=**Von Hand abgebrochen** im Board, in Phase `{status.get('phase') or '?'}`. Abgebrochene Läufe: {runs}.\n\n"
           f"Branch, Pull Request und Zustand bleiben. Weder der Orchestrator noch ein Kommentar startet das Ticket "
           f"wieder. Fortsetzen: im Board **Neu starten**, oder das Label `{ABORTED}` entfernen und `factory:go` setzen."
           f"\n\n<!-- software-factory -->")
    word = "1 Lauf" if len(cancelled) == 1 else f"{len(cancelled)} Läufe"
    return {"ok": True, "cancelled": cancelled,
            "message": f"#{number} ist abgebrochen: {word} gestoppt." if cancelled else f"#{number} ist abgebrochen; es lief gerade kein Lauf."}


STATIC = {"/theme.css": "text/css; charset=utf-8", "/theme.js": "text/javascript; charset=utf-8",   # shared by both pages
          "/views.css": "text/css; charset=utf-8", "/views.js": "text/javascript; charset=utf-8"}   # the line views of the board


# What only the open ticket's drawer reads. The board sends it for one ticket at a time (/api/card):
# for every ticket at once it is most of the answer, and the page asks for that answer every few seconds.
DRAWER_ONLY = ("activity", "timeline", "review", "acceptance", "mandate", "tasks", "failures", "notes", "run_urls")


def wire(value):
    """The board answer as the page gets it: each ticket's workflow without the drawer's details."""
    if not isinstance(value, dict) or not isinstance(value.get("cards"), list):
        return value
    cards = []
    for c in value["cards"]:
        w = c.get("workflow")
        if isinstance(w, dict):
            lite = {k: v for k, v in w.items() if k not in DRAWER_ONLY}
            lite["stations"] = [{**{k: v for k, v in st.items() if k != "runs"}, "run_tokens": _run_tokens(st.get("runs"))}
                                for st in w.get("stations") or []]
            c = {**c, "workflow": lite}
        cards.append(c)
    return {**value, "cards": cards}


def _run_tokens(runs) -> int:
    """All tokens of a station's runs: what the overview table shows without the runs themselves."""
    return sum(sum((r.get("tokens") or {}).get(k) or 0 for k in ("input", "output", "cache_read", "cache_write"))
               for r in runs or [] if isinstance(r, dict))


def tag_of(value) -> str:
    """Names one answer's content; the clock it carries does not count, so an unchanged board keeps its tag."""
    plain = {k: v for k, v in value.items() if k != "updated"} if isinstance(value, dict) else value
    return '"' + hashlib.sha1(json.dumps(plain, sort_keys=True).encode()).hexdigest()[:20] + '"'


class Snapshot:
    """The board's newest answer, made again in the background so that no page waits for GitHub.

    A request gets what is there at once; if that is older than max_age, a new one is made behind it.
    Only the very first request after the board starts waits. One GitHub read serves every tab."""

    def __init__(self, make, max_age: float = 7.0):
        self.make, self.max_age = make, max_age
        self.cond = threading.Condition()
        self.busy, self.at, self.value, self.body, self.gz, self.tag = False, 0.0, None, b"", b"", ""

    def _run(self) -> None:
        try:
            value = self.make()
            sent = wire(value)
            body, tag = json.dumps(sent).encode(), tag_of(sent)
            with self.cond:
                if tag != self.tag:   # unchanged content: keep the packed bytes, skip packing them again
                    self.body, self.gz, self.tag = body, gzip.compress(body, 5), tag
                self.value, self.at = value, time.time()
        except Exception as exc:   # keep the last answer; the next request tries again
            print(f"[factory] board: {exc}", flush=True)
        finally:
            with self.cond:
                self.busy = False
                self.cond.notify_all()

    def kick(self) -> None:
        """Make a new answer in the background, unless one is being made already."""
        with self.cond:
            if self.busy:
                return
            self.busy = True
        threading.Thread(target=self._run, name="factory-board", daemon=True).start()

    def get(self, max_age: float | None = None, wait: float = 120) -> "Snapshot":
        """The newest answer. Too old: start a new one; return the old one at once when there is one
        and no max_age was asked for, else wait until the answer is young enough."""
        limit = self.max_age if max_age is None else max_age
        asked = time.time()
        if asked - self.at > limit:
            self.kick()
            if self.value is None or max_age is not None:
                with self.cond:
                    self.cond.wait_for(lambda: self.at >= asked - limit or not self.busy, timeout=wait)
        return self

    def stale(self) -> None:
        """Something changed (a restart, a project removed): make a new answer now."""
        self.at = 0.0
        self.kick()



def serve(repos: list[str], local: list[str], branch: str, port: int, conf: dict | None = None) -> None:
    conf = conf or {}
    min_free = float(conf.get("min_free_disk_gb", 15))
    keepalive.start(repos, conf)

    def page() -> bytes:   # read on every request: a changed page shows without restarting the board
        return (ROOT / "factory" / "board.html").read_text().replace("__TOKEN__", token).encode()
    tickets: dict = {}   # (source, number) -> (time, body)
    pictures: dict = {}  # (source, number, file, hash) -> bytes: a picture never changes under its hash
    reading: set = set()   # tickets being read again in the background
    pending: dict = {}   # (source, number) -> when this board restarted the ticket
    snap = Snapshot(lambda: board(repos, local, branch, pending, min_free))
    snap.kick()   # ready before the first page asks
    # The machine page: this machine's load and each runner's share, read every few seconds (factory/machine.py).
    cfg_cache: dict = {"at": 0.0, "cfg": None}

    def machine_cfg() -> dict:
        if cfg_cache["cfg"] is None or time.time() - cfg_cache["at"] > 30:   # settings changed on the page apply soon
            try:
                cfg_cache["cfg"] = config.load_config()
            except Exception:
                cfg_cache["cfg"] = cfg_cache["cfg"] or {}
            cfg_cache["at"] = time.time()
        return cfg_cache["cfg"]
    monitor = machine.Monitor(machine_cfg).start()
    cleaner = cleanup.Cleaner()

    def runners_now() -> list[dict]:
        return (monitor.tick() or {}).get("runners") or []
    maps = Maps(branch, Path.home() / ".cache" / "software-factory" / "board", machine_cfg)
    # The watch reads the same answer, so a notification costs no GitHub read of its own.
    watch.start(lambda: snap.get(max_age=float(conf.get("watch_seconds", 90)) / 2).value["cards"], conf)

    def ticket_body(source: str, number: int) -> bytes:
        """Sessions of one ticket. Only sources this board was started with are read."""
        kind, _, ref = source.partition(":")
        if kind == "gh" and ref in repos:
            files, dirs = github_ticket_files(ref, branch, number)
        elif kind == "local" and ref.isdigit() and int(ref) < len(local):
            files, dirs = local_ticket_files(Path(local[int(ref)]).expanduser(), number)
        else:
            raise KeyError(source)
        return json.dumps({"stations": sessions(files, dirs)}).encode()

    # The settings page can change which commands run, so its requests must come from this page on this
    # machine: the token is only readable by a page served from here, and the Host check stops DNS rebinding.
    token = _page_token()
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Host") not in hosts:
                return self.send_error(403)
            url = urlparse(self.path)
            if url.path == "/api/board":
                got = snap.get()
                if got.value is None:
                    return self.send_error(503, "Das Board liest noch")
                if (self.headers.get("If-None-Match") or "") == got.tag:
                    return self._unchanged(got.tag)   # the page has this answer already: nothing to send, nothing to draw
                self._send(got.body, "application/json", got.gz, tag=got.tag)
            elif url.path == "/api/card":
                # One ticket's whole workflow, for its drawer.
                query = parse_qs(url.query)
                try:
                    source, number = query["source"][0], int(query["number"][0])
                    card = next(c for c in (snap.get().value or {}).get("cards") or []
                                if c["source"] == source and c["number"] == number)
                    self._send(json.dumps(card.get("workflow") or {}).encode(), "application/json")
                except (KeyError, ValueError, IndexError, StopIteration):
                    self.send_error(404)
            elif url.path == "/api/ticket":
                query = parse_qs(url.query)
                try:
                    key = (query["source"][0], int(query["number"][0]))
                    if key not in tickets:
                        tickets[key] = (time.time(), ticket_body(*key))
                    elif time.time() - tickets[key][0] > 7 and key not in reading:
                        # an older answer serves now; the new one is read behind it for the next ask
                        reading.add(key)

                        def again(key=key):
                            try:
                                tickets[key] = (time.time(), ticket_body(*key))
                            except Exception:
                                pass
                            finally:
                                reading.discard(key)
                        threading.Thread(target=again, daemon=True).start()
                    self._send(tickets[key][1], "application/json")
                except (KeyError, ValueError, IndexError):
                    self.send_error(404)
                except Exception as exc:
                    self._send(json.dumps({"error": str(exc)[:300]}).encode(), "application/json")
            elif url.path == "/api/evidence":
                # Only pictures the ticket's evidence.json names, from a source this board was started with.
                query = parse_qs(url.query)
                try:
                    source, number, name = query["source"][0], int(query["number"][0]), query["file"][0]
                    item = next(i for c in (snap.get().value or {}).get("cards") or []
                                if c["source"] == source and c["number"] == number
                                for i in (c.get("evidence") or {}).get("items") or [] if i["file"] == name)
                    key = (source, number, name, item.get("hash"))
                    if key not in pictures:
                        kind, _, ref = source.partition(":")
                        if kind == "gh" and ref in repos:
                            data = evidence_bytes(ref, branch, number, name)
                        elif kind == "local" and ref.isdigit() and int(ref) < len(local):
                            data = (Path(local[int(ref)]).expanduser() / "tickets" / str(number) / "evidence" / name).read_bytes()
                        else:
                            raise KeyError(source)
                        if len(pictures) > 300:
                            pictures.clear()
                        pictures[key] = data
                    self._picture(pictures[key], IMAGE_TYPES[Path(name).suffix.lower()])
                except (KeyError, ValueError, IndexError, StopIteration, OSError):
                    self.send_error(404)
                except Exception as exc:
                    self.send_error(502, str(exc)[:200])
            elif url.path == "/api/judge":
                # For the owner at this machine only (Host and the page's origin): the implementer never sees it.
                query = parse_qs(url.query)
                try:
                    source, number = query["source"][0], int(query["number"][0])
                    kind, _, repo = source.partition(":")
                    if kind != "gh" or repo not in repos:
                        raise KeyError(source)
                    self._send(json.dumps(judge_session(repo, number)).encode(), "application/json")
                except (KeyError, ValueError, IndexError):
                    self.send_error(404)
            elif url.path == "/api/backlog":
                query = parse_qs(url.query)
                try:
                    kind, _, repo = query["source"][0].partition(":")
                    run = query["run"][0]
                    if kind != "gh" or repo not in repos or not re.fullmatch(r"\d{8}T\d{6}Z", run):
                        raise KeyError(run)
                    files, dirs = github_state_files(repo, branch, f"orchestrator/backlog/{run}")
                    self._send(json.dumps(backlog_session(files, dirs)).encode(), "application/json")
                except (KeyError, IndexError):
                    self.send_error(404)
                except Exception as exc:
                    self._send(json.dumps({"error": str(exc)[:300]}).encode(), "application/json")
            elif url.path == "/api/settings":
                self._json(lambda: settings.state(repos))
            elif url.path == "/api/settings/project":
                repo = (parse_qs(url.query).get("repo") or [""])[0]
                self._json(lambda: settings.read_project(repo) if repo in repos else {"error": "Unbekanntes Projekt."})
            elif url.path == "/api/connect/repos":
                self._json(lambda: {"repos": connect.my_repos()})
            elif url.path == "/api/connect/inspect":
                query = parse_qs(url.query)
                self._json(lambda: connect.inspect((query.get("repo") or [""])[0], (query.get("base") or [""])[0] or None, repos))
            elif url.path in ("/", "/index.html") or url.path.startswith("/p/"):   # overview and one board per project
                self._send(page(), "text/html; charset=utf-8")
            elif url.path in STATIC:
                self._send((ROOT / "factory" / url.path[1:]).read_bytes(), STATIC[url.path])
            elif url.path == "/settings":
                html = (ROOT / "factory" / "settings.html").read_text().replace("__TOKEN__", token)
                self._send(html.encode(), "text/html; charset=utf-8")
            elif url.path.startswith("/map/"):   # /map/owner/name[?ticket=N]: the project's floor plan
                repo = url.path[len("/map/"):].strip("/")
                ticket = (parse_qs(url.query).get("ticket") or [""])[0]
                if repo not in repos or (ticket and not ticket.isdigit()):
                    return self.send_error(404)
                try:
                    self._send(maps.page(repo, int(ticket) if ticket else None), "text/html; charset=utf-8")
                except Exception as exc:
                    note = html_escape(str(exc)[:400])
                    body = (f"<!doctype html><meta charset=utf-8><title>Karte</title><link rel=stylesheet href=/theme.css>"
                            f"<main style='max-width:640px;margin:60px auto;padding:0 16px'><h1>Keine Karte</h1>"
                            f"<p>{note}</p><p><a href='/p/{html_escape(repo)}'>Zurück zum Board</a></p></main>").encode()
                    self.send_response(404)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            elif url.path == "/machine":
                html = (ROOT / "factory" / "machine.html").read_text().replace("__TOKEN__", token)
                self._send(html.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/live":
                self._json(monitor.tickets)
            elif url.path == "/api/cleanup":
                def look():
                    kind, _, repo = (parse_qs(url.query).get("source") or [""])[0].partition(":")
                    if kind != "gh" or repo not in repos:
                        return {"error": "Aufräumen gibt es nur für Projekte auf GitHub."}
                    if parse_qs(url.query).get("full"):   # the board's section: everything, removable or not
                        return {**cleanup.inventory_cached(repo, runners_now, bool(parse_qs(url.query).get("fresh"))),
                                "status": cleaner.status(repo)}
                    return {**cleanup.plan(repo, runners_now()), "status": cleaner.status(repo)}
                self._json(look)
            elif url.path == "/api/machine":
                self._json(lambda: monitor.state((snap.value or {}).get("cards") or []))
            else:
                self.send_error(404)

        def do_POST(self):
            if self.headers.get("Host") not in hosts or not secrets.compare_digest(
                    self.headers.get("X-Factory-Token") or "", token):
                return self.send_error(403)
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 1_000_000:
                return self.send_error(413)
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                return self.send_error(400)
            url = urlparse(self.path)
            if url.path == "/api/settings":
                def save():
                    result = settings.save_global(body.get("values"), body.get("raw"))
                    if result["ok"] and body.get("publish"):
                        result.update(settings.publish())
                    return result
                self._json(save)
            elif url.path == "/api/settings/project":
                self._json(lambda: settings.save_project(body.get("repo"), body.get("overrides"), body.get("sha"))
                           if body.get("repo") in repos else {"error": "Unbekanntes Projekt."})
            elif url.path == "/api/connect/remove":
                def unlink():
                    result = connect.remove_from_board(body.get("repo") or "", repos)
                    snap.stale()
                    return result
                self._json(unlink)
            elif url.path == "/api/orchestrate":
                def run_now():
                    kind, _, repo = str(body.get("source") or "").partition(":")
                    if kind != "gh" or repo not in repos:
                        return {"error": "Nur Projekte auf GitHub haben einen Orchestrator."}
                    result = keepalive.kick(repo)
                    if result == "unsupported":
                        return {"error": "Die factory.yml des Projekts kennt den Start von Hand noch nicht (Eingabe orchestrate)."}
                    if result != "ok":
                        return {"error": result}
                    keepalive.STATE.setdefault(repo, {})["kicked"] = time.time()
                    snap.stale()
                    return {"ok": True, "message": "Der Orchestrator ist angestoßen; sein Lauf erscheint in etwa einer Minute."}
                self._json(run_now)
            elif url.path == "/api/restart":
                def again():
                    kind, _, repo = str(body.get("source") or "").partition(":")
                    if kind != "gh" or repo not in repos:
                        return {"error": "Lokale Tickets startest du mit `python3 -m factory local` neu."}
                    result = restart(repo, int(body.get("number")))
                    pending[(f"gh:{repo}", int(body.get("number")))] = time.time()
                    snap.stale()
                    return result
                self._json(again)
            elif url.path == "/api/rework":
                def again_judge():
                    kind, _, repo = str(body.get("source") or "").partition(":")
                    if kind != "gh" or repo not in repos:
                        return {"error": "Nachbessern geht nur bei Tickets auf GitHub."}
                    result = rework(repo, int(body.get("number")))
                    pending[(f"gh:{repo}", int(body.get("number")))] = time.time()
                    snap.stale()
                    return result
                self._json(again_judge)
            elif url.path == "/api/proposal":
                def answer():
                    kind, _, repo = str(body.get("source") or "").partition(":")
                    if kind != "gh" or repo not in repos:
                        return {"error": "Vorschläge gibt es nur bei Projekten auf GitHub."}
                    result = proposal(repo, int(body.get("number")), str(body.get("action") or ""))
                    if body.get("action") == "start":
                        pending[(f"gh:{repo}", int(body.get("number")))] = time.time()
                    snap.stale()
                    return result
                self._json(answer)
            elif url.path == "/api/abort":
                def stop():
                    kind, _, repo = str(body.get("source") or "").partition(":")
                    if kind != "gh" or repo not in repos:
                        return {"error": "Ein lokaler Lauf endet mit Strg+C in seinem Terminal."}
                    result = abort(repo, int(body.get("number")), branch)
                    pending.pop((f"gh:{repo}", int(body.get("number"))), None)
                    snap.stale()
                    return result
                self._json(stop)
            elif url.path == "/api/cleanup":
                def clean():
                    kind, _, repo = str(body.get("source") or "").partition(":")
                    if kind != "gh" or repo not in repos:
                        return {"error": "Aufräumen gibt es nur für Projekte auf GitHub."}
                    return cleaner.start(repo, runners_now, str(body.get("runner") or "") or None)
                self._json(clean)
            elif url.path == "/api/machine/kill":
                self._json(lambda: monitor.kill(int(body.get("pid") or 0)))
            elif url.path == "/api/connect":
                def link():
                    result = connect.connect(body.get("repo") or "", body, repos)
                    snap.stale()   # the board shows the new project on its next refresh
                    return result
                self._json(link)
            else:
                self.send_error(404)

        def _json(self, make):
            try:
                result = make()
            except settings.SettingsError as exc:
                result = {"error": str(exc)}
            except (SystemExit, Exception) as exc:
                result = {"error": str(exc)[:600]}
            self._send(json.dumps(result).encode(), "application/json")

        def _unchanged(self, tag: str):
            self.send_response(304)
            self.send_header("ETag", tag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

        def _send(self, body: bytes, kind: str, gz: bytes | None = None, tag: str = ""):
            # Board answers are large JSON; packed they are a tenth of the size.
            packed = "gzip" in (self.headers.get("Accept-Encoding") or "") and (gz is not None or len(body) > 2048)
            if packed:
                body = gz if gz is not None else gzip.compress(body, 5)
            self.send_response(200)
            self.send_header("Content-Type", kind)
            if packed:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            if tag:
                self.send_header("ETag", tag)
            self.send_header("Cache-Control", "no-cache" if tag else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _picture(self, body: bytes, kind: str):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=86400")   # the URL carries the picture's hash
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    print(f"[factory] Board: http://127.0.0.1:{port}  (Strg+C beendet)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
