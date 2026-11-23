"""Disk space a project's factory work holds on this machine, and giving it back.

Every self-hosted runner of a project keeps a checkout of it (_work/<name>/<name>) and the factory's scratch
folder (_work/_temp/factory) with one worktree per ticket. Over many tickets the checkout's git store grows
pack by pack (each fetch of ticket branches and of the state branch adds one), and a worktree a broken run left
behind stays. On a large Unity project that was 4 to 12 GB per runner, against under 1 GB for the project's own CI
checkout of the same repository.

Only runners that are idle are touched: a running job owns its checkout and worktree. What stays: the checkout
itself and the folders git ignores in it (warm caches such as an engine's import cache), which the next ticket
would otherwise rebuild slowly.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path

NICE = ["nice", "-n", "19"]


def _git(repo: Path, *args: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=timeout)


def _size(path: Path) -> int:
    try:
        out = subprocess.run(["du", "-sk", str(path)], text=True, capture_output=True, timeout=300).stdout.split()
        return int(out[0]) * 1024
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0


def _git_store(checkout: Path) -> dict:
    """Size of the git store and how many packs it is split into (many packs: a repack gives space back)."""
    out = _git(checkout, "count-objects", "-v", timeout=120).stdout
    values = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
    kib = sum(int(values.get(k, 0)) for k in ("size", "size-pack", "size-garbage"))
    return {"bytes": kib * 1024, "packs": int(values.get("packs", 0))}


def unsaved(wt: Path) -> str:
    """Why a worktree must stay: work in it that is not on GitHub yet. Empty when it can go."""
    if not (wt / ".git").exists():
        return ""
    if _git(wt, "status", "--porcelain", timeout=120).stdout.strip():
        return "hat Änderungen, die nicht committet sind"
    branch = _git(wt, "rev-parse", "--abbrev-ref", "HEAD", timeout=60).stdout.strip()
    remote = _git(wt, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", timeout=60)
    if remote.returncode != 0:
        return f"Branch {branch} ist nicht auf GitHub"
    if _git(wt, "rev-list", "--count", f"origin/{branch}..HEAD", timeout=60).stdout.strip() not in ("", "0"):
        return "hat Commits, die nicht gepusht sind"
    return ""


TIDY_PACKS = 30   # a fresh clone has one or two packs; the factory's runners reached 90 to 123


def tidy(checkout: Path) -> str:
    """After a factory job on a self-hosted runner: repack the checkout's git store once it is split into many packs.
    actions/checkout turns git's own housekeeping off (gc.auto 0), so without this the store only grows."""
    if not (checkout / ".git").exists():
        return "kein Checkout"
    packs = _git_store(checkout)["packs"]
    if packs <= TIDY_PACKS:
        return f"{packs} Pakete, nichts zu tun"
    _git(checkout, "worktree", "prune", timeout=300)
    subprocess.run([*NICE, "git", "-C", str(checkout), "gc", "--prune=now", "--quiet"], capture_output=True, text=True,
                   timeout=3600)
    return f"{packs} Pakete neu gepackt zu {_git_store(checkout)['packs']}"


def checkout_of(runner: dict, repo: str) -> Path:
    name = repo.split("/", 1)[1]
    return Path(runner["folder"]) / "_work" / name / name


def scratch_of(runner: dict) -> Path:
    return Path(runner["folder"]) / "_work" / "_temp" / "factory"


def plan(repo: str, runners: list[dict]) -> dict:
    """What can go, per runner of the project, with sizes. Nothing is changed."""
    rows = []
    for r in runners:
        if r.get("repo") != repo or not (scratch_of(r).is_dir() or (checkout_of(r, repo) / ".git").exists()):
            continue   # holds nothing of this project
        checkout, scratch = checkout_of(r, repo), scratch_of(r)
        busy = r.get("busy")
        job = r.get("job") or {}
        row = {"runner": r["name"], "busy": bool(busy),
               "why_not": (f"arbeitet gerade ({job.get('label') or 'Job'}"
                           + (f" #{job['ticket']}" if job.get("ticket") else "") + ")") if busy else "",
               "items": [], "kept": []}
        if busy:   # a running job owns all of it: nothing to measure or offer
            rows.append(row)
            continue
        if checkout.is_dir():
            store = _git_store(checkout)
            row["items"].append({"kind": "git", "path": str(checkout / ".git"), "bytes": store["bytes"],
                                 "text": f"Git-Speicher des Checkouts ({store['packs']} Pakete) neu packen"})
        for wt in sorted(scratch.glob("wt-*")):
            keep = unsaved(wt)
            if keep:
                row["kept"].append(f"{wt.name}: {keep}")
                continue
            row["items"].append({"kind": "worktree", "path": str(wt), "bytes": _size(wt),
                                 "text": f"Worktree {wt.name}: alles darin liegt auf GitHub, der nächste Lauf holt es neu"})
        judge = checkout / "judge-out"
        if judge.is_dir():
            row["items"].append({"kind": "judge", "path": str(judge), "bytes": _size(judge),
                                 "text": "Reste eines Richter-Laufs"})
        rows.append(row)
    return {"repo": repo, "runners": rows, "t": time.time()}


def _clean_runner(row: dict, repo: str, runner: dict) -> list[str]:
    done = []
    checkout = checkout_of(runner, repo)
    for item in row["items"]:
        path = Path(item["path"])
        if item["kind"] == "worktree":
            if checkout.is_dir():
                _git(checkout, "worktree", "remove", "--force", str(path), timeout=600)
            shutil.rmtree(path, ignore_errors=True)
            done.append(f"{runner['name']}: {path.name} entfernt")
        elif item["kind"] == "judge":
            shutil.rmtree(path, ignore_errors=True)
            done.append(f"{runner['name']}: Richter-Reste entfernt")
    if checkout.is_dir():
        before = _git_store(checkout)["bytes"]
        _git(checkout, "worktree", "prune", timeout=300)
        _git(checkout, "remote", "prune", "origin", timeout=600)
        subprocess.run([*NICE, "git", "-C", str(checkout), "gc", "--prune=now", "--quiet"],
                       capture_output=True, text=True, timeout=3600)
        after = _git_store(checkout)["bytes"]
        done.append(f"{runner['name']}: Git-Speicher {before / 2**30:.1f} → {after / 2**30:.1f} GB")
    return done


def _ignored_dirs(checkout: Path) -> list[Path]:
    """Top-level folders git ignores in the checkout: warm caches (an engine's import cache, build output)."""
    out = _git(checkout, "status", "--ignored", "--porcelain", "--untracked-files=normal", timeout=300).stdout
    names = {line[3:].split("/", 1)[0] for line in out.splitlines() if line.startswith("!! ")}
    return sorted(p for p in (checkout / n for n in names) if p.is_dir())


_MEASURED: dict[str, tuple[float, dict]] = {}


def inventory_cached(repo: str, runners_of, fresh: bool = False, max_age: float = 90) -> dict:
    """inventory(), kept for a while: measuring every folder takes tens of seconds."""
    seen = _MEASURED.get(repo)
    if seen and not fresh and time.time() - seen[0] < max_age:
        return seen[1]
    result = inventory(repo, runners_of())
    _MEASURED[repo] = (time.time(), result)
    return result


def inventory(repo: str, runners: list[dict]) -> dict:
    """Everything the project leaves on this machine, per runner, with sizes and whether a cleanup may take it.

    Read only. Busy runners are measured too, but nothing of theirs is offered: their job owns it."""
    rows = []
    for r in runners:
        if r.get("repo") != repo or not Path(r.get("folder") or "").is_dir():
            continue
        checkout, scratch, busy = checkout_of(r, repo), scratch_of(r), bool(r.get("busy"))
        job = r.get("job") or {}
        hold = (f"Runner arbeitet gerade ({job.get('label') or 'Job'}" + (f" #{job['ticket']}" if job.get("ticket") else "") + ")") if busy else ""
        items = []

        def add(kind, path, size, text, can, why=""):
            items.append({"kind": kind, "path": str(path), "bytes": size, "text": text,
                          "removable": bool(can and not busy), "why": why or (hold if can and busy else "")})
        if checkout.is_dir():
            store = _git_store(checkout)
            add("git", checkout / ".git", store["bytes"], f"Git-Speicher ({store['packs']} Pakete)",
                store["packs"] > 2, "" if store["packs"] > 2 else "schon gepackt")
            caches = _ignored_dirs(checkout)
            measured = 0
            for c in caches:
                size = _size(c); measured += size
                if c.name != "judge-out" and size >= 2**20:   # judge-out has its own line; empty folders are noise
                    add("cache", c, size, f"Cache {c.name}", False, "bleibt: der nächste Lauf bräuchte sonst lange")
            rest = _size(checkout) - store["bytes"] - measured
            add("checkout", checkout, max(0, rest), "Arbeitskopie des Projekts", False, "bleibt: jeder Lauf startet darin")
        if scratch.is_dir():
            for wt in sorted(scratch.glob("wt-*")):
                ticket = wt.name[3:]
                if busy and str(job.get("ticket")) == ticket:
                    add("worktree", wt, _size(wt), f"Worktree für #{ticket}", False, "in Benutzung")
                    continue
                keep = unsaved(wt)
                add("worktree", wt, _size(wt), f"Worktree für #{ticket}", not keep, f"bleibt: {keep}" if keep else "")
        judge = checkout / "judge-out"
        if judge.is_dir() and (size := _size(judge)) >= 2**20:
            add("judge", judge, size, "Reste eines Richter-Laufs", True)
        diag = Path(r["folder"]) / "_diag"
        if diag.is_dir():
            add("logs", diag, _size(diag), "Protokolle des Runners", False, "bleibt: verwaltet der Runner selbst")
        rows.append({"runner": r["name"], "busy": busy, "job": hold, "factory": scratch.is_dir(),
                     "bytes": sum(i["bytes"] for i in items), "removable": sum(i["bytes"] for i in items if i["removable"]),
                     "items": items})
    return {"repo": repo, "runners": rows, "t": time.time(), "free": shutil.disk_usage(str(Path.home())).free}


class Cleaner:
    """One cleanup at a time per project, in the background; the board asks for its state."""

    def __init__(self):
        self.state: dict[str, dict] = {}
        self.lock = threading.Lock()

    def status(self, repo: str) -> dict:
        with self.lock:
            return dict(self.state.get(repo) or {"running": False})

    def start(self, repo: str, runners_of, only: str | None = None) -> dict:
        """Clean every idle runner of the project, or with only, that one runner."""
        with self.lock:
            if (self.state.get(repo) or {}).get("running"):
                return {"ok": False, "error": "Für dieses Projekt wird schon aufgeräumt."}
            self.state[repo] = {"running": True, "started": time.time(), "done": [], "skipped": []}

        def work():
            done, skipped = [], []
            try:
                free_before = shutil.disk_usage(str(Path.home())).free
                runners = runners_of()
                for row in plan(repo, runners)["runners"]:
                    if only and row["runner"] != only:
                        continue
                    if row["busy"]:
                        skipped.append(f"{row['runner']}: {row['why_not']}")
                        continue
                    # look again right before: a job may have started since the plan was made
                    now = next((r for r in runners_of() if r["name"] == row["runner"]), None)
                    if not now or now.get("busy"):
                        skipped.append(f"{row['runner']}: hat inzwischen einen Job")
                        continue
                    done += _clean_runner(row, repo, now)
                freed = shutil.disk_usage(str(Path.home())).free - free_before
                result = {"freed": max(0, freed)}
            except Exception as exc:   # the board shows it; the next cleanup can try again
                result = {"error": str(exc)[:300]}
            _MEASURED.pop(repo, None)   # sizes changed: the board's section measures again
            with self.lock:
                self.state[repo] = {"running": False, "finished": time.time(), "done": done, "skipped": skipped, **result}
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "message": "Aufräumen läuft im Hintergrund; das kann einige Minuten dauern."}
