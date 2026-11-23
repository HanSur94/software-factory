"""Parallel workers: independent tasks of one ticket run at the same time, each in its own folder.

The controller decides what may run together, from the plan's `depends_on` and
`files`; an agent is never asked to split work itself. Every worker gets its
own git worktree on its own branch, starting from the ticket branch as it
stands. When all workers of a wave are done, the controller puts their commits
on the ticket branch one after the other. A commit that does not fit on top of
the others is dropped, and its task runs again in the ticket's own folder,
where it sees what the others built.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import checks, gitops, stations, warm
from .context import Ctx


@dataclass
class Worker:
    task: dict
    path: Path
    branch: str
    sha: str | None = None           # the commit the worker left behind, if it changed anything
    error: BaseException | None = None


def _overlap(a: list[str], b: list[str]) -> bool:
    """Two tasks collide when one names a path the other names too, or a folder around it."""
    for x in (p.strip("/") for p in a):
        for y in (p.strip("/") for p in b):
            if x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False


def next_wave(tasks: list[dict], done: list[str], limit: int) -> list[dict]:
    """The tasks to work on next, in plan order: everything they depend on is done, and no two of them touch the
    same files. A task that does not say which files it touches runs on its own."""
    known = {t["id"] for t in tasks}
    open_tasks = [t for t in tasks if t["id"] not in done]
    ready = [t for t in open_tasks if all(d in done or d not in known for d in t.get("depends_on") or [])]
    if not ready:
        return open_tasks[:1]   # tasks that wait for each other in a circle: fall back to plan order
    wave: list[dict] = []
    for task in ready:
        files = task.get("files") or []
        if len(wave) >= max(1, limit):
            break
        if not wave:
            wave.append(task)
        elif files and all(t.get("files") and not _overlap(files, t["files"]) for t in wave):
            wave.append(task)
    return wave


def _folder(ctx: Ctx, task: dict) -> tuple[Path, str]:
    return ctx.workdir / f"wt-{ctx.ticket}-{task['id']}", f"{ctx.status['branch']}--{task['id']}"


def _remove(ctx: Ctx, path: Path, branch: str) -> None:
    gitops.git(ctx.repo, "worktree", "remove", "--force", str(path), check=False)
    gitops.git(ctx.repo, "worktree", "prune", check=False)
    gitops.git(ctx.repo, "branch", "-D", branch, check=False)


def clean_up(ctx: Ctx, tasks: list[dict]) -> None:
    """Remove worker folders an interrupted run left behind."""
    for task in tasks:
        path, branch = _folder(ctx, task)
        if path.exists() or gitops.git_ok(ctx.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            _remove(ctx, path, branch)
    ctx.status["workers"] = []


def _work(ctx: Ctx, worker: Worker, budget: float) -> None:
    task = worker.task
    try:
        stations.implement(ctx, worker.path, task, budget_cap=budget)
        with ctx.lock:   # git writes to the shared repository one at a time
            checks.protect_paths(ctx, worker.path)
            if gitops.commit_all(worker.path, f"{task['id']}: {task['title']}\n\nTicket: #{ctx.ticket}",
                                 ctx.cfg["delivery"]["author"]):
                worker.sha = gitops.head(worker.path)
    except BaseException as exc:   # reported by the controller once every worker has finished
        worker.error = exc


def run_wave(ctx: Ctx, wt: Path, wave: list[dict]) -> list[dict]:
    """Run the tasks of one wave at the same time. Returns the tasks whose result is on the ticket branch;
    a task whose commit collided with another one is run again here, in the ticket's own folder."""
    st, author = ctx.status, ctx.cfg["delivery"]["author"]
    base = gitops.head(wt)
    workers = []
    for task in wave:   # one after the other: creating worktrees takes a lock on the repository
        path, branch = _folder(ctx, task)
        _remove(ctx, path, branch)
        gitops.git(ctx.repo, "worktree", "add", "--quiet", "-b", branch, str(path), base)
        warm.seed(ctx.profile, ctx.repo, path)
        workers.append(Worker(task, path, branch))
    st["workers"] = [{"task": w.task["id"], "title": w.task["title"], "worktree": str(w.path), "branch": w.branch}
                     for w in workers]
    names = ", ".join(w.task["id"] for w in workers)
    ctx.emit("pipeline", "pipeline", outcome="parallel", detail=f"{names} gleichzeitig, jeder in einem eigenen Ordner")

    share = ctx.budget_left() / len(workers)   # together the workers may not spend more than the ticket has left
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        for worker in workers:
            pool.submit(_work, ctx, worker, share)

    merged, again = [], []
    for worker in workers:
        if worker.error is not None:
            continue
        if worker.sha is None or gitops.cherry_pick(wt, worker.sha, author):
            merged.append(worker.task)
        else:
            again.append(worker.task)
            ctx.emit("pipeline", "pipeline", outcome="parallel_conflict",
                     detail=f"{worker.task['id']} passte nicht auf die anderen Änderungen und läuft noch einmal nacheinander")
    for worker in workers:
        _remove(ctx, worker.path, worker.branch)
    st["workers"] = []
    st["done_tasks"] += [t["id"] for t in merged]

    failed = next((w.error for w in workers if w.error is not None), None)
    if failed is not None:
        raise failed
    for task in again:
        stations.implement(ctx, wt, task)
        checks.protect_paths(ctx, wt)
        gitops.commit_all(wt, f"{task['id']}: {task['title']}\n\nTicket: #{ctx.ticket}", author)
        st["done_tasks"].append(task["id"])
    return merged + again
