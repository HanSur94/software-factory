"""Agent stations. Each gets a fresh context, a prompt, and one contract file to write."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import contracts, events, evidence, fake, gitops, plugins
from . import harness as harnesses
from .agent import AgentResult, redact, run_agent, runtime_info
from .config import ROOT, fake_agent
from .context import Ctx
from .contracts import ContractError


class StationError(RuntimeError):
    def __init__(self, station: str, detail: str):
        super().__init__(f"{station}: {detail}")
        self.station, self.detail = station, detail


class BudgetExceeded(RuntimeError):
    pass


_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def _fill(text: str, values: dict) -> str:
    missing = sorted(set(_PLACEHOLDER.findall(text)) - set(values))
    if missing:
        raise KeyError(f"prompt needs values for: {', '.join(missing)}")
    # One pass, so that text taken from an issue is never scanned for placeholders.
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), text)


def commands_note(ctx: Ctx, rc: dict) -> str:
    """Without a sandbox the agent may start only some programs; everything else is refused. Agents that did not
    know concluded that a tool was broken ("the Unity editor could not be started") when it was only not allowed,
    and an available one (tools/ci/unity.sh) went unused."""
    if os.environ.get("FACTORY_SANDBOXED") == "1":
        return ""
    from .harness.claude import READ_ONLY_COMMANDS
    allowed = list(READ_ONLY_COMMANDS)
    if rc.get("tools", "read") in ("write", "run"):
        allowed += list(ctx.profile.get("agent_commands") or [])
    return ("\n## Programs you can start here\n\n"
            "This run has no sandbox. Besides reading and editing files, only these commands run; any other is "
            "refused. A refused command means not allowed, not broken: use one of these instead.\n\n"
            + "\n".join(f"- `{c}`" for c in allowed) + "\n")


def prompt_text(template: str, prompts: dict | None = None) -> str:
    """A station's prompt: the text set on the settings page (config `prompts`, keyed by the file), else the file."""
    own = (prompts or {}).get(template)
    return own if isinstance(own, str) and own.strip() else (ROOT / template).read_text()


def render(template: str, values: dict, prompts: dict | None = None) -> str:
    return _fill(prompt_text(template, prompts).rstrip() + "\n\n" + prompt_text(COMMON, prompts), values)


COMMON = "stations/_common.md"


def _bullets(items) -> str:
    return "\n".join(f"- {item}" for item in items) or "- (none)"


def _log_dir(ctx: Ctx) -> Path:
    log_dir = ctx.log_dir or ctx.run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _log(ctx: Ctx, station: str, result: AgentResult, n: int) -> None:
    log_dir = _log_dir(ctx)
    state = "ok" if result.ok else f"error: {result.error}"
    (log_dir / f"{n:02d}-{station}.md").write_text(
        f"model: {result.model}\ncost_usd: {result.cost_usd:.4f}\nduration_s: {result.duration_s:.0f}\n"
        f"state: {state}\n\n{redact(result.text[:6000])}\n")


def skills_note(rc: dict, usable: list[str], h) -> str:
    """The prompt's part about the station's skills. A skill with a condition (roles.<role>.skill_when) is
    loaded only when the run reaches it, since every skill read costs context; one without is always used."""
    if not usable:
        return ""
    when = {k: v for k, v in (rc.get("skill_when") or {}).items() if k in usable and v}
    return ("\n## Skills for this station\n\n"
            + "\n".join(f"- {s}: {when[s]}" if s in when else f"- {s}" for s in usable) + "\n\n"
            + h.skill_instruction(bool(when)))


def run_station(ctx: Ctx, role: str, template: str, values: dict, cwd: Path, out: Path | None, loader, *,
                readonly: bool, station: str | None = None, model: str | None = None,
                effort: str | None = None, extra_dirs: tuple = (), sensitive: bool = False,
                task: str | None = None, budget_cap: float | None = None):
    """Run one station: fresh agent, then validate the contract file it left behind.

    A broken file gets one repair attempt; a refusal or failure moves on to the
    role's fallback model. Everything is recorded as an event.
    """
    rc = ctx.cfg["roles"][role]
    name = station or role
    attempts = [(model or rc["model"], effort or rc.get("effort"))]
    if not model and rc.get("fallback"):
        attempts.append((rc["fallback"], rc.get("effort")))

    loaded = [n for n in plugins.needed_by(rc) if plugins.available(ctx.cfg, ctx.workdir, n)]
    skills = list(rc.get("skills") or []) + (list(rc.get("sensitive_skills") or []) if sensitive else [])
    usable = [s for s in skills if ":" not in s or s.split(":", 1)[0] in loaded]
    prompt = render(template, values, ctx.cfg.get("prompts")) + commands_note(ctx, rc) + evidence.note(ctx, role)
    h = harnesses.for_role(ctx.cfg, role)
    prompt += skills_note(rc, usable, h)
    entry = rc.get("entry") or ""
    if entry and entry[1:].split(":", 1)[0] in loaded:
        prompt = f"{h.entry(entry, _fill(rc.get('entry_args', ''), values))}\n\n{prompt}"

    if out is not None and out.exists():
        out.unlink()  # a file left over from an earlier round must not pass as this round's result
    ctx.checkpoint(name)
    where = runtime_info()

    sessions = ctx.run_dir / "sessions"
    sessions.mkdir(exist_ok=True)
    session = {"file": None}

    def on_tool(tool: str, text: str) -> None:
        # The live view of the board. For the judge this file stays inside the judge job.
        line = {"ts": events.now(), "station": name, "round": ctx.status.get("round", 1), "tool": tool, "text": text}
        if task:
            line["task"] = task
        with ctx.lock, open(ctx.run_dir / "activity.jsonl", "a") as fh:
            fh.write(redact(json.dumps(line, ensure_ascii=False)) + "\n")
        ctx.publish()

    def on_event(happened: dict) -> None:
        # The whole session of this run: what the agent was asked, said, called and got back.
        with open(session["file"], "a") as fh:
            fh.write(redact(json.dumps({"ts": events.now(), **happened}, ensure_ascii=False)) + "\n")
    error = "no attempt made"
    for attempt_model, attempt_effort in attempts:
        repair = ""
        for _ in range(2):
            # A run that the budget left cannot pay a quarter of is spent for nothing: one ticket's
            # reviewer started with 0.23 $ of its 2.50 $, ran into the cap and stopped with a technical error.
            floor = min(float(rc.get("budget_usd", 2.0)), budget_cap or float("inf")) / 4
            if ctx.budget_left() <= 0 or ctx.budget_left() < floor:
                raise BudgetExceeded()
            seq = ctx.next_seq(len(list(_log_dir(ctx).glob("*.md"))))
            session["file"] = sessions / f"{seq:02d}-{name}.jsonl"
            if fake_agent():
                fake.run(role, values, cwd, out)
                result = AgentResult(True, text="dry run", model="fake")
            else:
                on_event({"type": "prompt", "text": (prompt + repair)[:12000]})
                result = run_agent(
                    ctx.cfg, ctx.profile, role, prompt + repair, cwd, model=attempt_model, effort=attempt_effort,
                    budget_usd=min(float(rc.get("budget_usd", 2.0)), ctx.budget_left(), budget_cap or float("inf")),
                    dirs=[ctx.run_dir, *extra_dirs, *evidence.dirs(ctx, role)], env_extra=evidence.env(ctx, role),
                    plugin_dirs=[plugins.plugin_root(ctx.workdir) / n for n in loaded], on_tool=on_tool,
                    on_event=on_event,
                )
            ctx.spend(result.cost_usd)
            ctx.emit(name, "agent", model=result.model or attempt_model, effort=attempt_effort, harness=result.harness or h.name,
                     cost_usd=round(result.cost_usd, 4), duration_s=round(result.duration_s, 1),
                     outcome="ok" if result.ok else "error", detail=None if result.ok else result.error[:200],
                     skills_offered=usable, skills_used=result.skills, turns=result.turns,
                     tool_calls=result.tool_calls, subagents=result.subagents, tools=result.tools, runtime=where,
                     tokens=result.tokens or None, context_peak=result.context_peak or None,
                     context_window=result.context_window or None, subscription=result.subscription or None,
                     rtk=result.rtk or None, seq=seq, task=task)
            _log(ctx, name, result, seq)
            if readonly and not gitops.is_clean(cwd):
                gitops.discard_changes(cwd)
                ctx.emit(name, "guard", outcome="reverted", detail="read-only station changed the working tree")
            if not result.ok:
                error = result.error
                break  # try the fallback model
            if out is None:
                return result, None
            try:
                return result, loader(out)
            except ContractError as exc:
                error = str(exc)
                repair = f"\n\n---\nYour previous attempt failed the check: {error}\nWrite the file again so that it fits.\n"
    raise StationError(name, error)


# ---- the stations -----------------------------------------------------------

# What intake and planner do with a point the ticket leaves open (pipeline.questions).
BLOCKERS = ("something only the owner can provide: a program or device that is not in the repository and cannot be "
            "installed from it (a game, a licence, hardware), an account or a secret, or a decision where a wrong "
            "guess would be expensive to undo (deleting or migrating existing data, a public interface others rely on)")
NEEDS_INFO = {
    "ask": ("- `needs_info`: something is missing that changes what gets built. Ask at most three questions, each "
            "answerable in a sentence. Do not ask about things a planner can decide from the code."),
    "assume": ("- `needs_info`: only when the work cannot start without " + BLOCKERS + ". Ask at most three questions. "
               "Everything else you decide yourself: take the most reasonable reading of the issue, set `ready`, and "
               "write each such choice under `assumptions`. The owner prefers a pull request with a stated assumption "
               "to a ticket that waits."),
}
QUESTIONS = {
    "ask": ("If the ticket leaves a decision open that changes what gets built and that you cannot settle from the "
            "code, put it under `questions` (at most three) instead of guessing. The ticket then waits for the owner."),
    "assume": ("If the ticket leaves a decision open that you cannot settle from the code, decide it yourself, the way "
               "that is easiest to change later, and write the decision under `assumptions`. Use `questions` (at most "
               "three) only for " + BLOCKERS + ". The ticket then waits for the owner."),
}


def policy(ctx: Ctx) -> str:
    return ctx.cfg["pipeline"].get("questions") or "ask"


def intake(ctx: Ctx, cwd: Path, issue: dict) -> dict:
    out = ctx.run_dir / "ticket.json"
    answers = [c["body"] for c in issue["comments"] if c["by_owner"]]
    values = {
        "issue_number": ctx.ticket, "issue_title": issue["title"], "issue_body": issue["body"] or "(empty)",
        "owner_comments": "\n\n".join(answers) or "(none)", "out": out, "needs_info_rule": NEEDS_INFO[policy(ctx)],
    }
    _, ticket = run_station(ctx, "intake", ctx.cfg["roles"]["intake"]["prompt"], values, cwd, out,
                            lambda p: contracts.read_json(p, "ticket"), readonly=True)
    ticket["id"] = ctx.ticket
    contracts.write_json(out, ticket, "ticket")
    return ticket


def plan(ctx: Ctx, cwd: Path, feedback: str | None) -> dict:
    out = ctx.run_dir / "plan.md"
    block = "This is the first plan for this ticket."
    if feedback:
        if out.exists():
            out.replace(ctx.run_dir / "plan.prev.md")
        block = (f"A previous plan exists at {ctx.run_dir / 'plan.prev.md'} and was sent back. "
                 f"Revise it; keep what was not criticised.\n\nFeedback:\n{feedback}")
    values = {"ticket_path": ctx.run_dir / "ticket.json", "out": out, "feedback_block": block, "ticket": ctx.ticket,
              "questions_rule": QUESTIONS[policy(ctx)]}
    _, loaded = run_station(ctx, "planner", ctx.cfg["roles"]["planner"]["prompt"], values, cwd, out,
                            lambda p: contracts.read_frontmatter(p, "plan"), readonly=True)
    return loaded[0]


def load_plan(ctx: Ctx) -> dict:
    return contracts.read_frontmatter(ctx.run_dir / "plan.md", "plan")[0]


def plan_check(ctx: Ctx, cwd: Path, ticket: dict) -> dict:
    out = ctx.run_dir / "plan-review.json"
    values = {"ticket_path": ctx.run_dir / "ticket.json", "plan_path": ctx.run_dir / "plan.md",
              "size": ticket["size"], "out": out}
    _, review = run_station(ctx, "plan_check", ctx.cfg["roles"]["plan_check"]["prompt"], values, cwd, out,
                            lambda p: contracts.read_json(p, "plan-review"), readonly=True)
    return review


def _check_commands(ctx: Ctx) -> str:
    return _bullets(f"`{c['run']}`" for c in ctx.profile["checks"])


def implement(ctx: Ctx, cwd: Path, task: dict, budget_cap: float | None = None) -> None:
    values = {
        "ticket_path": ctx.run_dir / "ticket.json", "plan_path": ctx.run_dir / "plan.md",
        "task_id": task["id"], "task_title": task["title"], "task_goal": task["goal"],
        "task_acceptance": _bullets(task["acceptance"]), "task_files": _bullets(task.get("files") or []),
        "checks": _check_commands(ctx), "notes_path": ctx.run_dir / f"notes-{task['id']}.md",
    }
    run_station(ctx, "implementer", ctx.cfg["roles"]["implementer"]["prompt"], values, cwd, None, None,
                readonly=False, task=task["id"], budget_cap=budget_cap)


def rework(ctx: Ctx, cwd: Path, source: str, findings: str, escalate: bool) -> None:
    hint = ""
    if source == "judge":
        hint = ("These findings come from acceptance scenarios you cannot see; you get the observed symptom only. "
                "Reproduce each symptom yourself first, find its cause, and fix the cause. A fix that only "
                "special-cases the symptom will fail the next scenario.")
    elif source == "base":
        hint = ("A merge of the newest base branch into this branch is open and stopped at conflicts. Resolve every "
                "conflict so that both sides keep working: the base's change and this ticket's change. Read what the "
                "base changed (`git log -p HEAD..MERGE_HEAD -- <file>`) before you choose; never just take one side "
                "wholesale. Remove every conflict marker, leave the result uncommitted and do not abort the merge.")
    elif source == "ci":
        hint = ("These findings come from the project's own CI on the pull request: workflows that need a machine "
                "you do not have here (a licensed engine, a GPU). You cannot run them. Read the log for the first real error, "
                "fix its cause in the code, and say in your notes what you could not verify yourself.")
    values = {
        "ticket_path": ctx.run_dir / "ticket.json", "plan_path": ctx.run_dir / "plan.md", "source": source,
        "findings": findings, "source_hint": hint, "checks": _check_commands(ctx),
        "notes_path": ctx.run_dir / f"notes-rework-{ctx.status['rework_rounds'] + 1}.md",
    }
    esc = ctx.cfg["roles"].get("escalation") or {}
    run_station(ctx, "implementer", ctx.cfg["roles"]["implementer"]["rework_prompt"], values, cwd, None, None,
                readonly=False, station="rework",
                model=esc.get("model") if escalate else None, effort=esc.get("effort") if escalate else None)


def triage(ctx: Ctx, cwd: Path, failed: list) -> str:
    out = ctx.run_dir / "triage.md"
    values = {"failed": _bullets(f"{r.name} (exit code {r.code}): log at {r.log}" for r in failed), "out": out}
    _, text = run_station(ctx, "checks", ctx.cfg["roles"]["checks"]["prompt"], values, cwd, out,
                          contracts.read_text, readonly=True, station="triage")
    return text


def _earlier_review(out: Path) -> str:
    """The blocking points of the review before this one, if the change went back for them. Each review is a fresh
    agent: without the earlier points it checked none of them on purpose and found new ones instead, so a ticket
    could go 1, 3, 2 open points without either side knowing what was settled (seen on a real ticket)."""
    if not out.exists():
        return ""
    try:
        review = contracts.read_frontmatter(out, "review")[0]
    except contracts.ContractError:
        return ""
    if review.get("verdict") != "changes" or not review.get("blocking"):
        return ""
    points = "\n".join(f"- {f.get('file') or ''}{':' + str(f['line']) if f.get('line') else ''} {f['text']}".replace("-  ", "- ")
                       for f in review["blocking"])
    return ("## The previous review round\n\n"
            "The change went back to the implementer for these blocking points:\n\n" + points + "\n\n"
            "Check each of them first, yourself, in the current change: resolved or still open. A point still open "
            "stays blocking. Then review the rest as usual; a new blocking point needs the same concrete case as any "
            "other, and say in its text that it is new.\n")


def _evidence_note(ctx: Ctx) -> str:
    """What the implementer can produce. Reviews asked for screenshots and Unity runs on runners that had no Unity,
    and the ticket went round until it stopped (seen on two real tickets): a demand nobody can meet is no finding."""
    if os.environ.get("FACTORY_SANDBOXED") == "1":
        return ""
    from .harness.claude import READ_ONLY_COMMANDS
    allowed = list(READ_ONLY_COMMANDS) + list(ctx.profile.get("agent_commands") or [])
    return ("## What the implementer can produce\n\n"
            "The implementer can edit files and start only these programs:\n\n"
            + "\n".join(f"- `{c}`" for c in allowed) + "\n\n"
            "Evidence that none of them can produce (a screenshot of a running game, a manual playtest, a tool that "
            "is not listed) is never blocking: list it under `minor` as \"needs a human: ...\". Blocking evidence "
            "names the command that produces it.\n")


def review(ctx: Ctx, cwd: Path, ticket: dict) -> dict:
    out = ctx.run_dir / "review.md"
    sensitive = bool(ticket.get("sensitive"))
    values = {
        "earlier_review": _earlier_review(out),
        "ticket_path": ctx.run_dir / "ticket.json", "plan_path": ctx.run_dir / "plan.md",
        "base": ctx.status["base_sha"], "out": out,
        "evidence_note": _evidence_note(ctx),
        "sensitive_block": ("The ticket is marked sensitive: also review for security problems "
                            "(input handling, secrets, access checks, data loss)." if sensitive else ""),
    }
    _, loaded = run_station(ctx, "reviewer", ctx.cfg["roles"]["reviewer"]["prompt"], values, cwd, out,
                            lambda p: contracts.read_frontmatter(p, "review"), readonly=True, sensitive=sensitive)
    return loaded[0]


def judge(ctx: Ctx, cwd: Path, manifest: Path, out: Path, scenarios_dir: Path) -> dict:
    values = {"manifest_path": manifest, "out": out,
              "judge_notes": (ctx.profile.get("judge") or {}).get("notes") or "(none)",
              # The judge only runs once the work job's checks passed on the commit it gets (pipeline: checks, push,
              # then ready_for_judge). One judge spent 11 of its 12 minutes running that suite a second time.
              "checks_passed": _bullets(f"`{c['run']}` ({c['name']})" for c in ctx.profile.get("checks") or [])}
    _, result = run_station(ctx, "judge", ctx.cfg["roles"]["judge"]["prompt"], values, cwd, out,
                            lambda p: contracts.read_json(p, "judge-result"), readonly=False,
                            extra_dirs=(scenarios_dir,))
    return result
