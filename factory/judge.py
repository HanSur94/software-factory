"""The holdout judge. Runs in its own job; only verdict.json leaves it."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from . import checks, contracts, events, evidence, gitops, stations
from .context import Ctx
from .stations import BudgetExceeded, StationError

WITHHELD = "Fehlerbild zurückgehalten: Es enthielt Text aus dem Szenario."


def safe_symptom(symptom: str, scenario_text: str, window: int = 40) -> str:
    """A failure picture must not carry the scenario back to the implementer."""
    flat = " ".join((symptom or "").split())[:500]
    if not flat:
        return "ohne Beschreibung"
    haystack = " ".join(scenario_text.split()).lower()
    needle = flat.lower()
    if len(needle) >= window and any(needle[i:i + window] in haystack for i in range(len(needle) - window + 1)):
        return WITHHELD
    return flat


def generating(ctx: Ctx) -> bool:
    """A project without a scenario repository still gets a judge: by default it writes its scenarios itself."""
    return bool((ctx.cfg["verify"].get("generated_scenarios") or {}).get("enabled", False))


def generate(ctx: Ctx, private: Path) -> tuple[Path, list[Path]]:
    """Scenarios from the owner's issue alone, written by a fresh agent in an empty directory: it never sees the
    code, the plan or the factory's reading of the ticket, and the implementer never sees what it writes. The
    scenarios stay on the judge's runner like a holdout repository would."""
    issue = ctx.host.issue()
    blind = private / "scenario-writer"
    blind.mkdir(parents=True, exist_ok=True)
    if not (blind / ".git").exists():   # the read-only guard of a station compares git states
        gitops.git(blind, "init", "--quiet")
    issue_path, out = private / "issue.json", private / "scenarios.json"
    issue_path.write_text(json.dumps({"title": issue["title"], "body": issue["body"]}, ensure_ascii=False, indent=1))
    count = int((ctx.cfg["verify"].get("generated_scenarios") or {}).get("count", 5))
    _, written = stations.run_station(ctx, "scenario_writer", ctx.cfg["roles"]["scenario_writer"]["prompt"],
                                      {"issue_path": issue_path, "out": out, "count": count}, blind, out,
                                      lambda p: contracts.read_json(p, "scenarios"), readonly=True)
    root = private / "generated"
    files = []
    for n, sc in enumerate(written["scenarios"][:count], 1):
        path = root / "scenarios" / f"{n:02d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join([f"# {sc['title']}", "", sc["user"], "", "## Steps",
                                    *[f"{i}. {step}" for i, step in enumerate(sc["steps"], 1)], "",
                                    "## Satisfied when", *[f"- {w}" for w in sc["satisfied_when"]], ""]))
        files.append(path)
    return root, files


KEEP_ROUNDS = 5       # judge runs kept per ticket
KEEP_DAYS = 60        # a ticket's runs are dropped after this many days


def archive_root() -> Path:
    """Where judge runs are kept: on the judge's machine, never on GitHub, so no agent can read them there."""
    return Path(os.environ.get("FACTORY_JUDGE_ARCHIVE") or "~/.software-factory/judge").expanduser()


def archive_dir(repo: str, ticket: int) -> Path:
    return archive_root() / repo.replace("/", "__") / str(ticket)


def keep_session(ctx: Ctx, out: Path, scenario_files: list[Path], scenarios: Path | None, now: float | None = None,
                 target: Path | None = None) -> Path | None:
    """Keep the judge's session, its scenarios and its result on this machine, for the owner's board.

    The owner sees it on the board, live and after each round. The implementer never does: the archive is never
    uploaded, and the factory gives no agent its path. On a GitHub-hosted runner it disappears with the machine."""
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    if not repo or not (ctx.cfg["verify"].get("keep_judge_sessions", True)):
        return None
    private = out / "private"
    target = target or archive_dir(repo, ctx.ticket) / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(archive_root(), 0o700)
    for name in ("logs", "sessions"):
        if (private / name).is_dir():
            shutil.copytree(private / name, target / name, dirs_exist_ok=True)
    for name in ("events.jsonl", "judge-result.json"):
        if (private / name).is_file():
            shutil.copy2(private / name, target / name)
    if (out / "verdict.json").is_file():
        shutil.copy2(out / "verdict.json", target / "verdict.json")
    if scenario_files:
        (target / "scenarios").mkdir(exist_ok=True)
        manifest = _json_or_none(private / "manifest.json") or []
        ids = {e["path"]: e["id"] for e in manifest}
        for n, path in enumerate(scenario_files, 1):
            label = str(path.relative_to(scenarios)) if scenarios and path.is_relative_to(scenarios) else path.name
            name = f"{n:02d}-{ids.get(str(path), 'S')}.md"
            (target / "scenarios" / name).write_text(f"<!-- {label} -->\n" + path.read_text(errors="replace"))
    (target / LIVE).unlink(missing_ok=True)   # the copy is complete: the round is no longer live
    prune(archive_dir(repo, ctx.ticket).parent, now)
    return target


def prune(repo_dir: Path, now: float | None = None) -> None:
    """Keep the newest rounds of each ticket, and nothing older than KEEP_DAYS."""
    now = now or time.time()
    for ticket in (d for d in repo_dir.iterdir() if d.is_dir()) if repo_dir.is_dir() else []:
        rounds = sorted(d for d in ticket.iterdir() if d.is_dir())
        for old in rounds[:-KEEP_ROUNDS]:
            shutil.rmtree(old, ignore_errors=True)
        if rounds and now - rounds[-1].stat().st_mtime > KEEP_DAYS * 86400:
            shutil.rmtree(ticket, ignore_errors=True)


def _json_or_none(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def run_judge(ctx: Ctx, product: Path, scenarios: Path | None, out: Path, evidence_from: Path | None = None) -> dict:
    """evidence_from: the ticket's state folder in a local run; the judge job reads the pictures from the state branch."""
    seen: dict = {"files": [], "scenarios": scenarios, "evidence_from": evidence_from}
    # A self-hosted runner keeps its workspace, and git does not remove this folder: without a fresh start the
    # round would keep the logs of earlier tickets' judges, and a judge that dies early would leave the last
    # ticket's verdict.json to be uploaded as this one's.
    shutil.rmtree(out, ignore_errors=True)
    try:
        target = open_round(ctx, out)
    except OSError as exc:
        print(f"[factory] judge round not announced to the board: {exc}", flush=True)
        target = None
    verdict = _judge(ctx, product, scenarios, out, seen)
    try:
        keep_session(ctx, out, seen["files"], seen["scenarios"], target=target)
    except OSError as exc:   # keeping it for the board must never cost the verdict
        print(f"[factory] judge session not kept: {exc}", flush=True)
    return verdict


LIVE = "live.json"


def open_round(ctx: Ctx, out: Path, now: float | None = None) -> Path | None:
    """Tell the owner's board on this machine where the judge works right now, so it can show the session live.
    The pointer leads to the judge's private folder on this runner; keep_session replaces it with the copy."""
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    if not repo or not (ctx.cfg["verify"].get("keep_judge_sessions", True)):
        return None
    target = archive_dir(repo, ctx.ticket) / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(archive_root(), 0o700)
    (target / LIVE).write_text(json.dumps({"private": str((out / "private").resolve()), "pid": os.getpid(),
                                           "started": events.now()}))
    return target


def _judge(ctx: Ctx, product: Path, scenarios: Path | None, out: Path, seen: dict) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    private = out / "private"  # stays on the judge's runner
    private.mkdir(exist_ok=True)
    ctx.run_dir, ctx.log_dir = private, private / "logs"

    threshold = float(ctx.cfg["verify"]["satisfaction_threshold"])
    verdict = {"schema": "verdict/1", "ticket": ctx.ticket, "commit": gitops.head(product), "threshold": threshold,
               "scenarios_total": 0, "scenarios_passed": 0, "satisfaction": None, "passed": None, "failures": []}

    pattern = (ctx.profile.get("judge") or {}).get("pattern") or "scenarios/**/*.md"
    files = sorted(p for p in scenarios.glob(pattern) if p.is_file()) if scenarios and scenarios.is_dir() else []
    if not files and generating(ctx):
        try:
            scenarios, files = generate(ctx, private)
        except (StationError, BudgetExceeded, contracts.ContractError):
            files = []
        if files:
            verdict["notes"] = ("Szenarien aus dem Issue erzeugt: ein eigener Agent schrieb sie, ohne Code und Plan "
                                "zu sehen; der Implementierer kennt sie nicht.")
    seen.update(files=files, scenarios=scenarios)
    if not files:
        verdict["notes"] = "Keine Szenarien gefunden; der Richter hat nicht geurteilt."
        return _finish(ctx, verdict, out)

    manifest = [{"id": "S" + hashlib.sha1(str(p.relative_to(scenarios)).encode()).hexdigest()[:8], "path": str(p)}
                for p in files]
    (private / "manifest.json").write_text(json.dumps(manifest, indent=2))
    verdict["scenarios_total"] = len(manifest)

    setup = checks.run_setup(ctx, product, private / "setup")
    if setup is not None and not setup.ok:
        verdict.update(satisfaction=0.0, passed=False,
                       failures=[{"id": "setup", "symptom": "Das Setup-Kommando des Projekts schlägt auf diesem Stand fehl."}])
        return _finish(ctx, verdict, out)

    evidence.for_judge(ctx, product, private, seen.get("evidence_from"))
    try:
        result = stations.judge(ctx, product, private / "manifest.json", private / "judge-result.json", scenarios)
    except (StationError, BudgetExceeded):
        verdict["notes"] = "Der Richter konnte nicht urteilen; Einzelheiten stehen nur im Richter-Job."
        return _finish(ctx, verdict, out)

    by_id = {r["id"]: r for r in result["results"]}
    for entry in manifest:
        r = by_id.get(entry["id"])
        if r and r["satisfied"]:
            verdict["scenarios_passed"] += 1
        else:
            symptom = r.get("symptom", "") if r else "Szenario wurde nicht geprüft."
            verdict["failures"].append({"id": entry["id"],
                                        "symptom": safe_symptom(symptom, Path(entry["path"]).read_text())})
    verdict["satisfaction"] = round(verdict["scenarios_passed"] / len(manifest), 4)
    verdict["passed"] = verdict["satisfaction"] >= threshold
    return _finish(ctx, verdict, out)


def _finish(ctx: Ctx, verdict: dict, out: Path) -> dict:
    verdict["cost_usd"] = round(ctx.status.get("spent_usd", 0.0), 4)
    verdict["model"] = ctx.cfg["roles"]["judge"]["model"]
    runs = [e for e in events.read(ctx.run_dir) if e["station"] == "judge" and e["kind"] == "agent"]
    if runs:
        verdict["duration_s"] = round(sum(e.get("duration_s", 0.0) for e in runs), 1)
        verdict["turns"] = sum(e.get("turns", 0) for e in runs)
        verdict["skills_offered"] = sorted({s for e in runs for s in e.get("skills_offered", [])})
        verdict["skills_used"] = sorted({s for e in runs for s in e.get("skills_used", [])})
        tools: dict = {}
        for e in runs:
            for name, count in (e.get("tools") or {}).items():
                tools[name] = tools.get(name, 0) + count
        verdict["tools"], verdict["runtime"] = tools, runs[-1].get("runtime") or {}
        tokens: dict = {}
        for e in runs:
            for name, count in (e.get("tokens") or {}).items():
                tokens[name] = tokens.get(name, 0) + count
        verdict["tokens"] = tokens
        verdict["context_peak"] = max((e.get("context_peak") or 0 for e in runs), default=0)
        verdict["context_window"] = runs[-1].get("context_window") or 0
        verdict["subscription"] = runs[-1].get("subscription") or {}
    contracts.write_json(out / "verdict.json", verdict, "verdict")
    shown = "-" if verdict["satisfaction"] is None else f"{verdict['satisfaction']:.2f}"
    print(f"[factory] #{ctx.ticket} judge: {verdict['scenarios_passed']}/{verdict['scenarios_total']} "
          f"scenarios, satisfaction {shown}, passed {verdict['passed']}", flush=True)
    return verdict
