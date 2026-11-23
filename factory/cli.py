"""Command line of the factory. Every stage is one command, so any scheduler can call it."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

import yaml

from . import backlog, contracts, deliver as delivery, floormap, gitops, harness, judge as judging, machine, orchestrator, pipeline, plugins, report
from .config import check, deep_merge, load_config, workdir
from .context import Ctx, new_status
from .hosts import GitHubHost, LocalHost
from .state import BranchState, LocalState

PROFILE = ".factory/profile.yaml"


def load_profile(repo: Path, ref: str) -> dict:
    try:
        text = gitops.git(repo, "show", f"{ref}:{PROFILE}")
    except gitops.GitError as exc:
        raise SystemExit(f"{PROFILE} not found at {ref}. A project needs a profile before the factory can work on it.") from exc
    return contracts.validate(yaml.safe_load(text), "profile")


def _base(repo: Path, local: bool) -> tuple[str, str]:
    """(ref the ticket branch starts from, name of the branch a pull request targets)"""
    if local:
        return "HEAD", gitops.git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = os.environ.get("FACTORY_BASE") or gitops.git(
        repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[-1]
    return f"origin/{branch}", branch


def make_ctx(args, *, local: bool, with_state: bool = True) -> Ctx:
    repo = Path(args.repo).resolve()
    base_ref, base_branch = _base(repo, local)
    profile = load_profile(repo, base_ref)
    cfg = check(deep_merge(load_config(), profile.get("overrides")), f"{PROFILE} (overrides)")
    if getattr(args, "budget", None):
        cfg["limits"]["budget_usd_per_ticket"] = args.budget
    wd = workdir(repo)
    ctx = Ctx(cfg=cfg, profile=profile, repo=repo, workdir=wd, ticket=args.issue, run_dir=wd,
              status=new_status(args.issue), base_ref=base_ref, base_branch=base_branch)
    if not with_state:
        return ctx
    ctx.store = LocalState(wd / "state") if local else BranchState(
        repo, cfg["storage"]["branch"], wd, cfg["delivery"]["author"])
    ctx.run_dir = ctx.store.open(args.issue)
    if (ctx.run_dir / "status.json").exists():
        ctx.status = contracts.read_json(ctx.run_dir / "status.json", "status")
    ctx.host = LocalHost(Path(args.issue_file), args.issue, ctx.run_dir) if local else GitHubHost(repo, args.issue)
    return ctx


def _output(**values) -> None:
    for key, value in values.items():
        print(f"[factory] {key}={value}", flush=True)
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a") as fh:
            fh.writelines(f"{k}={v}\n" for k, v in values.items())


def _measured(ctx, job: str, meter) -> None:
    """What the job used goes into the ticket's events: the board and the closing report show it."""
    try:
        res = meter.result()
        if res["minutes"] >= 1:
            ctx.emit("pipeline", "pipeline", outcome="resources", job=job, detail="Rechner: " + machine.describe(res), **res)
            ctx.publish(every=0)
    except Exception as exc:   # a measurement never decides a ticket
        print(f"[factory] could not record what the job used: {exc}", flush=True)


def cmd_work(args) -> int:
    ctx = make_ctx(args, local=False)
    with machine.Meter(ctx.cfg) as meter:
        outcome = pipeline.work(ctx)
    _measured(ctx, "work", meter)
    _output(status=outcome, branch=ctx.status.get("branch") or "")
    if outcome in ("waiting_human", "rejected", "failed", "done", "split", "aborted"):   # this ticket's slot is free again
        _hand_on(ctx)
    return 0


def _hand_on(ctx) -> None:
    try:
        decisions: list = []
        if orchestrator.start_queued(ctx, decisions):
            orchestrator.record(ctx, f"Ticket #{ctx.ticket} gab seinen Platz frei", decisions)
    except Exception as exc:   # the ticket's own result stands; the sweep tries again later
        print(f"[factory] could not start a waiting ticket: {exc}", flush=True)


def cmd_judge(args) -> int:
    ctx = make_ctx(args, local=False, with_state=False)
    ctx.host = GitHubHost(ctx.repo, args.issue)   # the scenario writer reads the issue
    scenarios = Path(args.scenarios).resolve() if args.scenarios else None
    judging.run_judge(ctx, Path(args.repo).resolve(), scenarios, Path(args.out).resolve())
    return 0


def _read_verdict(path: str | None) -> dict | None:
    if path and Path(path).exists():
        return contracts.read_json(Path(path), "verdict")
    return None


def cmd_deliver(args) -> int:
    ctx = make_ctx(args, local=False)
    _output(status=delivery.deliver(ctx, _read_verdict(args.verdict)))
    _hand_on(ctx)
    return 0


def cmd_orchestrate(args) -> int:
    """Look after every ticket of the project: restart what broke off, catch up on CI, start what waits."""
    from types import SimpleNamespace
    args.issue = 0   # the sweep is about every ticket, not one
    ctx = make_ctx(args, local=False, with_state=False)
    ctx.store = BranchState(ctx.repo, ctx.cfg["storage"]["branch"], ctx.workdir, ctx.cfg["delivery"]["author"])
    ctx.host = GitHubHost(ctx.repo, 0)
    started_at = time.time()
    done = orchestrator.sweep(ctx, lambda n: make_ctx(SimpleNamespace(repo=args.repo, issue=n, budget=None), local=False))
    _output(**{k: ",".join(str(x) for x in done[k]) for k in ("restarted", "ci", "conflicts", "started", "left")})
    helpers = {}
    if (ctx.cfg.get("backlog") or {}).get("enabled"):
        try:
            helpers["backlog"] = backlog.tick(ctx, ctx.host)
            _output(backlog=helpers["backlog"])
        except Exception as exc:   # a failed proposal round must not fail the sweep
            helpers["backlog"] = f"Fehler: {exc}"[:300]
            print(f"[factory] backlog planner: {exc}", flush=True)
    if (ctx.cfg.get("map") or {}).get("enabled"):
        try:   # last: the base branch's floor plan, drawn again once it moved (factory/floormap.py)
            helpers["map"] = floormap.tick(ctx)
            _output(map=helpers["map"])
        except Exception as exc:   # a map that could not be drawn must not fail the sweep
            helpers["map"] = f"Fehler: {exc}"[:300]
            print(f"[factory] floor plan: {exc}", flush=True)
    try:
        orchestrator.record(ctx, orchestrator.trigger(), done["decisions"], done["checked"], started_at, helpers, prune=True)
    except Exception as exc:   # the board misses one record; the sweep itself did its work
        print(f"[factory] could not record the sweep: {exc}", flush=True)
    return 0


def cmd_local(args) -> int:
    """The whole pipeline on this machine: no GitHub, state in a local folder."""
    ctx = make_ctx(args, local=True)
    if args.answer:
        ctx.host.answer(args.answer)
    plugins.fetch(ctx.cfg, ctx.workdir)
    outcome = pipeline.work(ctx)
    if outcome == "ready_for_judge":
        wt = ctx.workdir / f"wt-{ctx.ticket}"
        judge_ctx = make_ctx(args, local=True, with_state=False)
        judge_ctx.host = ctx.host   # the scenario writer reads the issue
        out = ctx.workdir / f"judge-{ctx.ticket}"
        verdict = judging.run_judge(judge_ctx, wt, Path(args.scenarios).resolve() if args.scenarios else None, out,
                                    evidence_from=ctx.run_dir)
        outcome = delivery.deliver(ctx, verdict)
    _output(status=outcome, branch=ctx.status.get("branch") or "", run_dir=ctx.run_dir,
            spent_usd=f"{ctx.status['spent_usd']:.2f}")
    return 0


def cmd_human_outcome(args) -> int:
    match = re.match(re.escape(load_config()["delivery"]["branch_prefix"]) + r"(\d+)-", args.branch)
    if not match:
        print(f"[factory] {args.branch} is not a factory branch", flush=True)
        return 0
    args.issue = int(match.group(1))
    ctx = make_ctx(args, local=False)
    delivery.human_outcome(ctx, args.merged == "true")
    return 0


def cmd_ci_result(args) -> int:
    """A workflow of the project finished on a branch; on a factory branch that is the CI of its pull request."""
    match = re.match(re.escape(load_config()["delivery"]["branch_prefix"]) + r"(\d+)-", args.branch)
    if not match:
        print(f"[factory] {args.branch} is not a factory branch", flush=True)
        _output(status="ignored")
        return 0
    args.issue = int(match.group(1))
    ctx = make_ctx(args, local=False)
    _output(status=delivery.ci_result(ctx, args.workflow, args.conclusion, args.run_id, args.sha))
    return 0


def cmd_route(args) -> int:
    """Which runner each job needs: the profile names a capability, the config says where it runs."""
    repo = Path(args.repo).resolve()
    profile = contracts.validate(yaml.safe_load((repo / PROFILE).read_text()), "profile")
    cfg = check(deep_merge(load_config(), profile.get("overrides")), f"{PROFILE} (overrides)")
    out = {}
    for job, default in cfg["runtime"]["jobs"].items():
        capability = (profile.get("runtime") or {}).get(job, default)
        target = cfg["runtime"]["capabilities"][capability]
        out[f"{job}_runner"] = json.dumps(target["runs_on"])
        out[f"{job}_sandboxed"] = "1" if target["provider"] == "github-hosted" else "0"
    judge = profile.get("judge") or {}
    out["scenarios_repo"] = judge.get("scenarios_repo", "")
    out["agent_version"] = cfg["agent"].get("version", "latest")
    # Every job installs the CLIs of the harnesses its roles run in: the judge job only the judge's.
    work_roles = [r for r in cfg["roles"] if r not in ("judge", "escalation")]
    out["work_install"] = " ".join(harness.install(cfg, work_roles))
    out["judge_install"] = " ".join(harness.install(cfg, ["judge"]))
    _output(**out)
    return 0


def cmd_report(args) -> int:
    repo = Path(args.repo).resolve()
    cfg = load_config()
    wd = workdir(repo)
    store = LocalState(wd / "state") if args.local else BranchState(repo, cfg["storage"]["branch"], wd, cfg["delivery"]["author"])
    print(report.render(report.summarize(cfg, report.collect(store.tickets_dir()))))
    return 0


def cmd_board(args) -> int:
    """Kanban board of all tickets, served on this machine."""
    from . import board
    cfg = load_config()
    conf = cfg.get("board") or {}
    repos = args.repos or conf.get("repos") or []
    local = args.local or conf.get("local") or []
    board.serve(repos, local, cfg["storage"]["branch"], args.port or conf.get("port", 8787), conf)
    return 0


def cmd_fetch_plugins(args) -> int:
    print("[factory] plugins:", ", ".join(plugins.fetch(load_config(), workdir(Path(args.repo).resolve()))) or "none")
    return 0


def cmd_validate(args) -> int:
    path = Path(args.file)
    try:
        if path.suffix == ".md":
            contracts.read_frontmatter(path, args.contract)
        elif path.suffix in (".yaml", ".yml"):
            contracts.validate(yaml.safe_load(path.read_text()), args.contract)
        else:
            contracts.read_json(path, args.contract)
    except contracts.ContractError as exc:
        print(f"{path}: {exc}")
        return 1
    print(f"{path}: fits {args.contract}/1")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="factory", description="Software Factory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *, issue=True):
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        p.add_argument("--repo", default=".", help="product checkout")
        if issue:
            p.add_argument("--issue", type=int, required=True)
            p.add_argument("--budget", type=float, help="budget in USD for this ticket")
        return p

    add("work", cmd_work)
    p = add("judge", cmd_judge)
    p.add_argument("--scenarios")
    p.add_argument("--out", required=True)
    add("deliver", cmd_deliver).add_argument("--verdict")
    p = add("local", cmd_local)
    p.add_argument("--issue-file", required=True)
    p.add_argument("--scenarios")
    p.add_argument("--answer", help="answer of the owner to an open question")
    p = add("human-outcome", cmd_human_outcome, issue=False)
    p.add_argument("--branch", required=True)
    p.add_argument("--merged", choices=["true", "false"], required=True)
    # The values come from the workflow_run event. On a runner they arrive as FACTORY_CI_* variables: a workflow
    # name has spaces and a branch name is chosen by whoever pushed it, so neither goes through a shell line.
    p = add("ci-result", cmd_ci_result, issue=False)
    for name in ("branch", "workflow", "conclusion", "run-id", "sha"):
        env = os.environ.get("FACTORY_CI_" + name.upper().replace("-", "_"))
        p.add_argument(f"--{name}", default=env or ("" if name == "sha" else None), required=env is None and name != "sha")
    add("orchestrate", cmd_orchestrate, issue=False)
    add("route", cmd_route, issue=False)
    add("report", cmd_report, issue=False).add_argument("--local", action="store_true")
    add("fetch-plugins", cmd_fetch_plugins, issue=False)
    p = sub.add_parser("board")
    p.set_defaults(fn=cmd_board)
    p.add_argument("--repos", nargs="*", help="OWNER/REPO, default from factory.yaml")
    p.add_argument("--local", nargs="*", help="local state folders")
    p.add_argument("--port", type=int)
    p = sub.add_parser("validate")
    p.set_defaults(fn=cmd_validate)
    p.add_argument("contract")
    p.add_argument("file")

    args = ap.parse_args(argv)
    if args.cmd in ("work", "judge", "local", "deliver", "ci-result") and threading.current_thread() is threading.main_thread():
        _end_with_the_run()
    try:
        return args.fn(args)
    finally:
        if args.cmd in ("work", "judge", "deliver", "ci-result", "orchestrate") and os.environ.get("RUNNER_ENVIRONMENT") == "self-hosted":
            _tidy(args.repo)


def _tidy(repo: str) -> None:
    """A self-hosted runner keeps its checkout between jobs: repack its git store when it has grown (cleanup.tidy)."""
    from . import cleanup
    try:
        print(f"[factory] git store: {cleanup.tidy(Path(repo).resolve())}", flush=True)
    except Exception as exc:   # housekeeping must never fail the job
        print(f"[factory] git store not tidied: {exc}", flush=True)


def _end_with_the_run() -> None:
    """A cancelled run (the board's abort, a cancel on GitHub) sends SIGINT, then SIGTERM. A check started its
    commands in their own session; without this they would outlive the run and hold the runner."""
    from . import checks

    def stop(signum, frame):
        checks.kill_running()
        signal.signal(signum, signal.SIG_DFL)
        raise SystemExit(128 + signum)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)


if __name__ == "__main__":
    sys.exit(main())
