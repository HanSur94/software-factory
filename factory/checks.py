"""Quick checks and guards. No model involved: these are plain commands and rules."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from . import events, gitops
from .agent import clean_env
from .context import Ctx


@dataclass
class CheckResult:
    name: str
    ok: bool
    code: int
    seconds: float
    log: Path


TAIL = 20000          # characters of output kept in a published log
GROUPS: set[int] = set()   # process groups of running checks: they have their own session, so a cancelled run
                           # does not take them down by itself (a Unity editor would go on for an hour)


def kill_running() -> None:
    for pgid in list(GROUPS):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        GROUPS.discard(pgid)
LIVE_EVERY = 5.0      # seconds between rewrites of a running check's log


def _write_state(log_dir: Path, state: dict) -> None:
    (log_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n")


def _run(ctx: Ctx, name: str, command: str, cwd: Path, timeout_minutes: float, log_dir: Path,
         state: dict | None = None, env: dict | None = None) -> CheckResult:
    """Run one command. Its output goes to <log_dir>/<name>.log while it runs, so the board can show it live."""
    started = time.time()
    log = log_dir / f"{name}.log"
    tail: deque[str] = deque()
    size = [0]
    entry = None
    if state is not None:
        entry = next(e for e in state["checks"] if e["name"] == name)
        entry.update(state="running", started=events.now())
        _write_state(log_dir, state)

    def write(footer: str) -> None:
        log.write_text(f"$ {command}\n{''.join(tail)}{footer}")

    proc = subprocess.Popen(["bash", "-c", command], cwd=str(cwd), text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env={**clean_env(ctx.cfg, ctx.profile), **(env or {})},
                            start_new_session=True, errors="replace")
    GROUPS.add(proc.pid)
    timed_out = threading.Event()

    def stop() -> None:   # the whole process group: a check may start its own children (Unity, a test runner)
        timed_out.set()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    timer = threading.Timer(timeout_minutes * 60, stop)
    timer.start()
    lock = threading.Lock()

    def read() -> None:
        for line in proc.stdout:
            with lock:
                tail.append(line)
                size[0] += len(line)
                while size[0] > TAIL and len(tail) > 1:   # keep the newest output only
                    size[0] -= len(tail.popleft())

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        # A check can be silent for minutes (a Unity suite): the log is rewritten on a clock, not per line.
        while proc.poll() is None:
            time.sleep(min(1.0, LIVE_EVERY))
            if proc.poll() is None:
                with lock:
                    write(f"\n[läuft seit {round(time.time() - started)} s]\n")
                ctx.publish()
        code = proc.wait()
        reader.join(timeout=5)
    finally:
        timer.cancel()
        GROUPS.discard(proc.pid)
    if timed_out.is_set():
        code = 124
        tail.append(f"\n[timed out after {timeout_minutes} minutes]")
    with lock:
        write(f"\n[exit code {code}]\n")
    seconds = round(time.time() - started, 1)
    if entry is not None:
        entry.update(state="ok" if code == 0 else "failed", seconds=seconds, code=code)
        _write_state(log_dir, state)
    return CheckResult(name, code == 0, code, seconds, log)


class DiskFull(Exception):
    """Too little free disk space for the work ahead (limits.min_free_disk_gb)."""


def wait_for_disk(ctx: Ctx, path: Path, wait_minutes: float = 20, poll_s: float = 30) -> None:
    """On a machine several tickets and the project's CI share, Unity imports and worktrees can fill the disk; once
    it is full every job on it fails at once. Before work that needs space a ticket waits for other jobs to free
    some, and stops (the orchestrator tries again later) instead of being the one that fills it."""
    need = float(ctx.cfg["limits"].get("min_free_disk_gb") or 0)
    if need <= 0:
        return
    deadline, waited = time.time() + wait_minutes * 60, False
    while True:
        free = shutil.disk_usage(path).free / 1e9
        if free >= need:
            if waited:
                ctx.emit("pipeline", "pipeline", outcome="disk_ok", detail=f"{free:.1f} GB frei")
            return
        if time.time() >= deadline:
            raise DiskFull(f"zu wenig freier Speicher: {free:.1f} GB frei, gebraucht werden {need:g} GB")
        if not waited:
            ctx.emit("pipeline", "pipeline", outcome="disk_wait", detail=f"{free:.1f} GB frei, warte auf {need:g} GB")
            ctx.publish()
            waited = True
        time.sleep(poll_s)


def run_setup(ctx: Ctx, cwd: Path, log_dir: Path, state: dict | None = None) -> CheckResult | None:
    if not ctx.profile.get("setup"):
        return None
    log_dir.mkdir(parents=True, exist_ok=True)
    return _run(ctx, "setup", ctx.profile["setup"], cwd, 20, log_dir, state)


def run_all(ctx: Ctx, cwd: Path) -> list[CheckResult]:
    log_dir = ctx.run_dir / "checks"
    log_dir.mkdir(parents=True, exist_ok=True)
    # What runs, in order, and how far it got: the board reads this while the checks run.
    todo = ([{"name": "setup", "run": ctx.profile["setup"]}] if ctx.profile.get("setup") else []) + \
           [{"name": c["name"], "run": c["run"]} for c in ctx.profile["checks"]]
    state = {"round": ctx.status.get("rework_rounds", 0) + 1, "checks": [{**c, "state": "waiting"} for c in todo]}
    for stale in log_dir.glob("*.log"):   # logs of an earlier round would pass as this round's
        stale.unlink()
    _write_state(log_dir, state)
    ctx.publish(every=0)
    setup = run_setup(ctx, cwd, log_dir, state)
    if setup is not None and not setup.ok:
        return [setup]
    results = []
    for c in ctx.profile["checks"]:
        results.append(_run(ctx, c["name"], c["run"], cwd, float(c.get("timeout_minutes", 10)), log_dir, state))
        ctx.publish(every=0)
    for r in results:
        ctx.emit("checks", "check", outcome="pass" if r.ok else "fail", detail=r.name, duration_s=r.seconds)
    return results


def protect_paths(ctx: Ctx, cwd: Path, restore: bool = False) -> list[str]:
    """Undo changes to paths the factory itself depends on (workflows, the project profile).

    restore=True: the branch is set back to the base's version before checks and review. At that point every
    change an agent made has already been through this guard, so what is undone here came from the machinery
    (a stale base, a merged base) and is recorded as such: it does not hold back an automatic merge."""
    protected = tuple(ctx.cfg["limits"].get("protected_paths") or [])
    base = ctx.status["base_sha"]
    touched = [f for f in gitops.changed_files(cwd, base) if f.startswith(protected)]
    for path in touched:
        if gitops.git_ok(cwd, "cat-file", "-e", f"{base}:{path}"):
            gitops.git(cwd, "checkout", base, "--", path)
        else:
            gitops.git(cwd, "rm", "-rf", "--quiet", "--cached", "--ignore-unmatch", path, check=False)
            target = cwd / path
            if target.is_file():
                target.unlink()
    if touched:
        if restore:
            ctx.emit("guard", "guard", outcome="restored", detail="restored to the base: " + ", ".join(touched)[:300])
        else:
            ctx.emit("guard", "guard", outcome="reverted", detail="protected paths: " + ", ".join(touched)[:300])
            ctx.note("Änderungen an geschützten Pfaden wurden verworfen: " + ", ".join(touched))
    return touched


def size_limit(ctx: Ctx, cwd: Path) -> None:
    limit = (ctx.cfg["verify"].get("limits") or {}).get("max_changed_lines")
    lines = gitops.changed_lines(cwd, ctx.status["base_sha"])
    ctx.status["changed_lines"] = lines
    if limit and lines > limit:
        ctx.emit("guard", "guard", outcome="over_limit", detail=f"{lines} changed lines, limit {limit}")
        ctx.note(f"Die Änderung umfasst {lines} Zeilen und liegt über dem Limit von {limit}.")
