"""The controller. Deliberately not an agent: a plain script that runs the stations
in a fixed order, counts loops and budget, and stops at a limit."""
from __future__ import annotations

import os
import re
import time
import traceback
import unicodedata
from pathlib import Path

from . import checks, contracts, events, evidence, floormap, gitops, orchestrator, split, stations, warm, workers
from .context import Ctx
from .stations import BudgetExceeded, StationError


class Park(Exception):
    """The ticket waits for the human."""

    def __init__(self, reason: str, since: str):
        self.reason, self.since = reason, since


class Stop(Exception):
    """The ticket cannot continue without the human."""


class ReviewOpen(Exception):
    """The review keeps asking for changes and the rework gets nowhere: the human decides on the pull request."""


class Aborted(Exception):
    """The owner aborted the ticket on the board (label factory:aborted)."""


def _check_abort(ctx: Ctx) -> None:
    """Between two phases: an aborted ticket goes no further, whichever run carries it."""
    labels = getattr(ctx.host, "current_labels", None)
    if labels and ctx.label("aborted") in labels():
        raise Aborted()


def _abort(ctx: Ctx) -> str:
    st = ctx.status
    st["stopped"], st["current"], st["queued"] = "Von Hand abgebrochen", None, None
    ctx.emit("human", "human", outcome="aborted", detail=f"in Phase {st['phase']}")
    try:
        ctx.host.labels(remove=[ctx.label("running"), ctx.label("queued")])
        ctx.save("aborted by the owner")
    except Exception as exc:   # the run ends either way
        print(f"[factory] could not record the abort: {exc}", flush=True)
    return "aborted"


class HandOver(Exception):
    """This run has worked long enough: a new run of the workflow continues the ticket."""


def _time_check(ctx: Ctx) -> None:
    """Called before new work starts. A run that goes on until the job's time limit is cut off in the middle of a
    task (ck3-battle-engine #59 after 3 hours, in its eighth task); stopping between tasks loses nothing."""
    limit = float(ctx.cfg["pipeline"].get("max_run_minutes") or 0)
    if limit and (time.time() - ctx.started) / 60 >= limit:
        raise HandOver()


def _slug(text: str) -> str:
    text = text.lower().translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40].strip("-") or "ticket"


def ensure_worktree(ctx: Ctx, title: str) -> Path:
    """Every ticket works in its own worktree on its own branch."""
    st = ctx.status
    wt = ctx.workdir / f"wt-{ctx.ticket}"
    branch = st.get("branch") or f"{ctx.cfg['delivery']['branch_prefix']}{ctx.ticket}-{_slug(title)}"
    if not (wt / ".git").exists():
        gitops.git(ctx.repo, "worktree", "prune")
        if ctx.host.remote and gitops.remote_has_branch(ctx.repo, branch):
            gitops.git_net(ctx.repo, "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
            gitops.git(ctx.repo, "worktree", "add", "--quiet", "-B", branch, str(wt), f"origin/{branch}")
        elif gitops.git_ok(ctx.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            gitops.git(ctx.repo, "worktree", "add", "--quiet", str(wt), branch)
        else:
            gitops.git(ctx.repo, "worktree", "add", "--quiet", "-b", branch, str(wt), ctx.base_ref)
            _lost_work(ctx, branch)
        warm.seed(ctx.profile, ctx.repo, wt)
    st["branch"], st["worktree"] = branch, str(wt)
    st["base_branch"] = st.get("base_branch") or ctx.base_branch
    st["base_sha"] = base_point(ctx, wt) or st.get("base_sha") or gitops.git(ctx.repo, "rev-parse", ctx.base_ref)
    return wt


def base_point(ctx: Ctx, wt: Path) -> str:
    """The newest commit of the base branch that the ticket branch contains. Everything after it is the
    ticket's own work: what the protected-path guard, the size limit and the reviewer look at. The base
    the ticket first started from is not enough: a branch made anew after it was lost, or one that has
    taken in the moved base branch, contains newer base commits, and those are not the ticket's changes."""
    base = gitops.git(ctx.repo, "rev-parse", ctx.base_ref, check=False)   # in the repository: "HEAD" there is the base
    return gitops.git(wt, "merge-base", "HEAD", base, check=False) if base else ""


AFTER_IMPLEMENT = ("checks", "rework_judge", "rework_ci", "rework_base", "judge")


def _lost_work(ctx: Ctx, branch: str) -> None:
    """The ticket branch had to be created anew, but the state says work was done on it: that work is gone
    (before every commit was pushed, a ticket that stopped on a throwaway runner lost its commits). Carrying on
    would review an empty branch against a finished plan, so the implementation starts again."""
    st = ctx.status
    if not st.get("done_tasks") and st.get("phase") not in AFTER_IMPLEMENT:
        return
    lost = ", ".join(st.get("done_tasks") or []) or "-"
    st["done_tasks"], st["rework_rounds"] = [], 0
    if st.get("phase") in AFTER_IMPLEMENT:
        st["phase"] = "implement"
    ctx.emit("pipeline", "guard", outcome="work_lost", detail=f"{branch} missing; tasks done before: {lost}")
    ctx.note(f"Der Branch `{branch}` mit der bisherigen Arbeit war nicht mehr da (Tasks {lost}); die Umsetzung beginnt neu.")


def _take_in_base(ctx: Ctx, wt: Path) -> None:
    """Every run starts on the newest base branch. A ticket can run for hours or be restarted days later; in the
    meantime the base gets new tools, new CI and fixes the ticket's checks and agents should use. Merging it in
    at the start, not only right before a merge, keeps the ticket working against today's project. On a conflict
    the branch stays as it was and the run goes on; the merge before the pull request is merged tries again."""
    base = gitops.git(ctx.repo, "rev-parse", ctx.base_ref, check=False)   # in the repository: "HEAD" there is the base
    if not base or gitops.git_ok(wt, "merge-base", "--is-ancestor", base, "HEAD"):
        return
    if ctx.status.get("phase") == "rework_base":
        return   # the phase itself merges, and resolves what conflicts (_resolve_base)
    if gitops.merge(wt, base, ctx.cfg["delivery"]["author"]):
        ctx.status["base_sha"] = base_point(ctx, wt) or ctx.status["base_sha"]
        ctx.emit("pipeline", "pipeline", outcome="base_merged", detail=ctx.base_ref)
        _keep_work(ctx, wt)
    else:
        ctx.note(f"Der neue Stand von `{ctx.base_branch}` ließ sich nicht ohne Konflikt einarbeiten; "
                 "das Ticket arbeitet auf dem älteren Stand weiter.")


def _keep_work(ctx: Ctx, wt: Path) -> None:
    """Push the ticket branch after every commit. A runner can be thrown away at any point (a GitHub-hosted one
    always is); work that only lives on it is lost when the ticket stops before its pull request."""
    try:
        ctx.host.push(wt, ctx.status["branch"])
    except gitops.GitError as exc:   # the next commit tries again; the ticket must not stop over this
        print(f"[factory] could not push {ctx.status['branch']}: {exc}", flush=True)


def _questions(items: list[str], intro: str) -> str:
    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(items, 1))
    return f"**Rückfrage der Factory**\n\n{intro}\n\n{numbered}\n\nAntworte hier im Issue; deine Antwort setzt das Ticket fort."


def _commit(ctx: Ctx, wt: Path, message: str, restore: bool = False) -> None:
    checks.protect_paths(ctx, wt, restore=restore)
    if gitops.commit_all(wt, f"{message}\n\nTicket: #{ctx.ticket}", ctx.cfg["delivery"]["author"]):
        _keep_work(ctx, wt)


def _resume(ctx: Ctx, issue: dict) -> bool:
    """Returns False while the ticket still waits for an answer."""
    st = ctx.status
    waiting = st.get("waiting")
    if waiting:
        answers = [c["body"] for c in issue["comments"] if c["by_owner"] and c["created_at"] >= waiting["since"]]
        if not answers:
            return False
        st["waiting"] = None
        ctx.feedback = "\n\n".join(answers)
        ctx.emit("human", "human", outcome="answered", detail=waiting["reason"])
        if waiting["reason"] == "plan_approval":
            if "/approve" in ctx.feedback:
                st["plan_approved"], st["phase"], ctx.feedback = True, "implement", None
            else:
                st["phase"] = "plan"
        elif waiting["reason"] == "intake_questions":
            st["phase"] = "intake"
        else:
            st["phase"] = "plan"
    if st.get("stopped"):
        # The owner started the ticket again after a stop: fresh budget, fresh loop counters. What the
        # owner wrote since the stop goes to the implementer as guidance.
        log = events.read(ctx.run_dir)
        since = st.get("stopped_at") or (log[-1]["ts"] if log else "")   # tickets stopped before stopped_at existed
        guidance = [c["body"] for c in issue["comments"] if c["by_owner"] and since and c["created_at"] >= since]
        if guidance:
            ctx.feedback = "\n\n".join(guidance)
        if st["phase"] == "judge":   # stopped because the judge stayed unsatisfied: one more round of rework
            st["phase"], st["round"] = "rework_judge", st["round"] + 1
        st["stopped"], st["budget_base"], st["rework_rounds"], st["ci_rounds"] = None, st["spent_usd"], 0, 0
        st.pop("open_findings", None)
        st.pop("review_stalled", None)
    return True


def owner_rework(ctx: Ctx) -> bool:
    """The owner asked for another round on a delivered ticket whose judge was not satisfied: the label `rework`
    (the board's button "Nachbessern" sets it). The ticket goes back to the implementer with the judge's findings
    and whatever the owner wrote since the pull request; the pull request is updated afterwards."""
    st, host, L = ctx.status, ctx.host, ctx.label
    if "rework" not in ctx.cfg["human"]["labels"]:
        return False
    issue = host.issue()
    if L("rework") not in issue["labels"]:
        return False
    verdict_file = ctx.run_dir / "verdict.json"
    verdict = contracts.read_json(verdict_file, "verdict") if verdict_file.exists() else None
    if not verdict or verdict.get("passed") is not False:
        host.labels(remove=[L("rework")])
        host.comment("**Nachbessern nicht möglich:** Zu diesem Ticket gibt es kein Richter-Urteil mit Fehlerbildern. "
                     "Schreib, was fehlt, in einen Kommentar und starte das Ticket über `" + L("go") + "` neu.")
        ctx.save("rework asked without a failed verdict")
        return False
    log = events.read(ctx.run_dir)
    since = next((e["ts"] for e in reversed(log) if e.get("outcome") == "delivered"), "")
    guidance = [c["body"] for c in issue["comments"] if c["by_owner"] and since and c["created_at"] >= since
                and not c["body"].startswith("**")]
    if guidance:
        ctx.feedback = "\n\n".join(guidance)
    st["round"] += 1
    st["phase"], st["rework_rounds"], st["budget_base"] = "rework_judge", 0, st["spent_usd"]
    host.labels(remove=[L("rework"), L("review")])
    host.comment(f"**Nachbessern:** Das Ticket geht mit den Fehlerbildern des Richters ({verdict['scenarios_passed']} "
                 f"von {verdict['scenarios_total']} Szenarien){' und deinen Hinweisen' if guidance else ''} zurück "
                 f"an den Implementierer (Runde {st['round']}). Danach wird {st.get('pr') or 'der Pull Request'} "
                 "aktualisiert und wartet wieder auf dich.")
    ctx.emit("pipeline", "pipeline", outcome="auto_restart", detail="Nachbessern nach dem Richter, vom Besitzer angestoßen")
    ctx.save("owner asked for rework after the judge")
    return True


def _split_parent(ctx: Ctx) -> str:
    """A ticket split into parts is built by its parts: a run of the ticket itself (from the queue, a restart on the
    board, a stray label) has nothing to do. It leaves the ticket as split, not stopped, and frees its slot."""
    st, L = ctx.status, ctx.label
    if st.get("stopped") and "Phase `split`" in st["stopped"]:   # older runs stopped here before this check existed
        st["stopped"] = None
    ctx.host.labels(add=[L("split")], remove=[L("running"), L("queued"), L("go"), L("needs_human")])
    st["queued"] = None
    ctx.save("split ticket: its parts do the work")
    print("[factory] ticket is split into parts; the parts do the work", flush=True)
    return "split"


def work(ctx: Ctx) -> str:
    """Run a ticket from wherever it stands up to the judge. Returns the outcome:
    ready_for_judge, waiting_human, queued, rejected, failed or done."""
    st, host, L = ctx.status, ctx.host, ctx.label
    if st["phase"] == "merged" or (st["phase"] == "delivered" and not owner_rework(ctx)):
        return "done"
    if st["phase"] == "split":
        return _split_parent(ctx)
    issue = host.issue()
    if ctx.label("aborted") in issue["labels"]:
        print("[factory] ticket was aborted by hand; the board's restart takes the label away", flush=True)
        return _abort(ctx)
    if not _resume(ctx, issue):
        print("[factory] ticket still waits for an answer from the owner", flush=True)
        return "waiting_human"
    reason = orchestrator.slot_reason(ctx)
    if reason:   # no free slot or no money left today: the ticket waits, and a run that ends starts it
        return orchestrator.queue(ctx, reason)
    st["queued"] = None
    if os.environ.get("FACTORY_DRY_RUN") == "1":   # a dry run is never merged (deliver.merge_blockers)
        st["dry_run"] = True
    host.labels(add=[L("running")], remove=[L("go"), L("waiting"), L("needs_human"), L("queued")])
    if os.environ.get("GITHUB_RUN_ID"):
        url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
               f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
        if url not in st.setdefault("run_urls", []):
            st["run_urls"].append(url)
    ctx.emit("pipeline", "pipeline", outcome="started", detail=st["phase"])
    try:
        wt = ensure_worktree(ctx, issue["title"])
        _take_in_base(ctx, wt)
        outcome = _run_phases(ctx, wt, issue)
        st["current"] = None
        ctx.save(f"work finished: {outcome}")
        return outcome
    except Aborted:
        return _abort(ctx)
    except HandOver:
        st["current"] = None
        ctx.emit("pipeline", "pipeline", outcome="handover",
                 detail=f"nach {round((time.time() - ctx.started) / 60)} Minuten übernimmt ein neuer Lauf")
        ctx.save("handed over to a new run")
        ctx.host.dispatch()
        return "continued"
    except Park as park:
        st["waiting"], st["current"] = {"reason": park.reason, "since": park.since}, None
        host.labels(add=[L("waiting")], remove=[L("running")])
        ctx.emit("human", "human", outcome="asked", detail=park.reason)
        ctx.save(f"waiting for the owner ({park.reason})")
        return "waiting_human"
    except (Stop, StationError, BudgetExceeded, contracts.ContractError, gitops.GitError, checks.DiskFull) as exc:
        reason = "Budget des Tickets aufgebraucht" if isinstance(exc, BudgetExceeded) else str(exc)
        return _stop(ctx, reason)
    except Exception:
        _stop(ctx, "unerwarteter Fehler im Steuerskript:\n```\n" + traceback.format_exc()[-1500:] + "\n```")
        raise


def _stop(ctx: Ctx, reason: str) -> str:
    L = ctx.label
    ctx.status["stopped"], ctx.status["current"] = reason[:500], None
    ctx.emit("pipeline", "pipeline", outcome="stopped", detail=reason[:200])
    try:
        ctx.status["stopped_at"] = ctx.host.comment(
            f"**Factory angehalten** in Phase `{ctx.status['phase']}`.\n\n{reason}\n\n"
            f"Bisher verbraucht: {ctx.status['spent_usd']:.2f} $. Das Label `{L('go')}` startet das Ticket an "
            "dieser Stelle neu; ein Kommentar von dir davor geht als Hinweis an den Implementierer.")
        ctx.host.labels(add=[L("needs_human")], remove=[L("running")])
        ctx.save("stopped")
    except Exception as exc:  # reporting must not hide the original problem
        print(f"[factory] could not report the stop: {exc}", flush=True)
    return "failed"


def _run_phases(ctx: Ctx, wt: Path, issue: dict) -> str:
    st, host, cfg = ctx.status, ctx.host, ctx.cfg["pipeline"]
    while True:
        _check_abort(ctx)
        phase = st["phase"]

        if phase == "intake":
            ticket = stations.intake(ctx, wt, issue)
            if ticket["status"] == "needs_info":
                since = host.comment(_questions(ticket["questions"], "Bevor geplant wird, fehlt noch:"))
                raise Park("intake_questions", since)
            if ticket.get("assumptions"):
                host.comment("**Annahmen der Factory** (statt Rückfragen)\n\n" + "\n".join(f"- {a}" for a in ticket["assumptions"])
                             + "\n\nDas Ticket läuft weiter; die Annahmen stehen auch im Pull Request.")
            if ticket["status"] == "rejected":
                host.comment(f"**Die Factory nimmt dieses Issue nicht an.**\n\n{ticket['reason']}")
                host.labels(add=[ctx.label("rejected")], remove=[ctx.label("running")])
                st["phase"] = "rejected"
                return "rejected"
            st["phase"] = "plan"

        elif phase == "plan":
            feedback, ctx.feedback, st["plan_feedback"] = ctx.feedback or st.get("plan_feedback"), None, None
            plan = stations.plan(ctx, wt, feedback)
            st["plan_rounds"] += 1
            st["plan_approved"] = False
            if plan.get("questions"):
                since = host.comment(_questions(plan["questions"], "Der Planer braucht eine Entscheidung:"))
                raise Park("plan_questions", since)
            st["phase"] = "plan_check"

        elif phase == "plan_check":
            plan = stations.load_plan(ctx)
            mode = ctx.mode("plan_check")
            if mode != "off":
                ticket = contracts.read_json(ctx.run_dir / "ticket.json", "ticket")
                result = stations.plan_check(ctx, wt, ticket)
                blocking = [f["text"] for f in result["findings"] if f["severity"] == "blocking"]
                ctx.emit("plan_check", "verdict", outcome=result["verdict"], detail=f"{len(blocking)} blocking")
                if result["verdict"] == "revise" and mode == "gate" and st["plan_rounds"] < cfg["max_plan_rounds"]:
                    st["plan_feedback"] = "\n".join(f"- {t}" for t in blocking) or "The plan check asked for a revision."
                    st["phase"] = "plan"
                    continue
            big = len(plan["tasks"]) > cfg["human_plan_approval_above"]["tasks"]
            if big and not st["plan_approved"] and stations.policy(ctx) == "assume":
                ctx.note(f"Der Plan hat {len(plan['tasks'])} Tasks und lief ohne Freigabe weiter (questions: assume).")
            elif big and not st["plan_approved"]:
                tasks = "\n".join(f"- **{t['id']}** {t['title']}" for t in plan["tasks"])
                since = host.comment(
                    f"**Plan zur Freigabe** ({len(plan['tasks'])} Tasks)\n\n{plan['summary']}\n\n{tasks}\n\n"
                    "Antworte mit `/approve`, um den Plan freizugeben. Jede andere Antwort geht als "
                    "Änderungswunsch an den Planer.")
                raise Park("plan_approval", since)
            if not st.get("dry_run") and split.wanted(ctx, plan, issue):
                split.split(ctx, plan, issue)
                st["phase"] = "split"
                return "split"
            st["phase"] = "implement"

        elif phase == "implement":
            tasks = stations.load_plan(ctx)["tasks"]
            workers.clean_up(ctx, tasks)   # folders of a run that was cut off
            while True:
                # Tasks that do not depend on each other and touch different files form a wave. Only as many run at
                # once as the budget left pays a full implementer run for: split thinner, every one of them runs
                # into its cap and the money is spent for nothing.
                per_run = float(ctx.cfg["roles"]["implementer"].get("budget_usd", 2.0))
                limit = max(1, min(int(cfg.get("parallel_tasks", 1)), int(ctx.budget_left() // per_run)))
                wave = workers.next_wave(tasks, st["done_tasks"], limit)
                if not wave:
                    break
                _time_check(ctx)
                checks.wait_for_disk(ctx, wt)
                if ctx.budget_left() < per_run / 2:
                    open_tasks = [t["id"] for t in tasks if t["id"] not in st["done_tasks"]]
                    raise Stop(
                        f"Das Restbudget von {ctx.budget_left():.2f} $ reicht nicht für die offenen Tasks "
                        f"{', '.join(open_tasks)}: ein Lauf des Implementierers darf bis {per_run:.2f} $ kosten. "
                        "Ein höheres `limits.budget_usd_per_ticket` im Profil oder ein kleineres Ticket hilft; "
                        f"`{ctx.label('go')}` startet es an dieser Stelle mit frischem Budget neu.")
                if len(wave) == 1:
                    task = wave[0]
                    stations.implement(ctx, wt, task)
                    _commit(ctx, wt, f"{task['id']}: {task['title']}")
                    st["done_tasks"].append(task["id"])
                else:
                    workers.run_wave(ctx, wt, wave)
                    _keep_work(ctx, wt)
            st["phase"] = "checks"

        elif phase == "rework_judge":
            verdict = contracts.read_json(ctx.run_dir / "verdict.json", "verdict")
            findings = "\n".join(f"- {f['symptom']}" for f in verdict["failures"])
            _rework(ctx, wt, "judge", findings)
            st["phase"] = "checks"

        elif phase == "rework_ci":
            # The project's CI was red on the pull request (deliver.ci_result): fix it, then the same way as before.
            _rework(ctx, wt, "ci", st.get("ci_findings") or "The project's CI failed on the pull request.")
            st["ci_findings"] = None
            st["phase"] = "checks"

        elif phase == "rework_base":
            # GitHub reported the pull request as conflicting with its base (deliver.base_conflict).
            _resolve_base(ctx, wt)
            st["phase"] = "checks"

        elif phase == "checks":
            # Whatever reached the branch, protected paths go to the review and the pull request as the base has
            # them: a commit made before the guard knew the right base must not slip through.
            _commit(ctx, wt, "restore protected paths", restore=True)
            _checks_and_review(ctx, wt)
            checks.size_limit(ctx, wt)
            host.push(wt, st["branch"])
            st["phase"] = "judge"
            return "ready_for_judge"

        elif phase == "judge":
            return "ready_for_judge"

        else:
            raise Stop(f"Ticket steht in Phase `{phase}` und kann von hier nicht weiterlaufen.")


def _resolve_base(ctx: Ctx, wt: Path) -> None:
    """Merge the newest base into the ticket branch and let the implementer resolve the conflict, as one more round
    of rework. Checks, review and judge then look at the merged result like at any other round. A conflict the
    implementer leaves unresolved stops the ticket, with the branch as it was."""
    base = gitops.git(ctx.repo, "rev-parse", ctx.base_ref, check=False)
    if not base or gitops.git_ok(wt, "merge-base", "--is-ancestor", base, "HEAD"):
        return
    author, before = ctx.cfg["delivery"]["author"], gitops.git(wt, "rev-parse", "HEAD")
    files = gitops.merge_open(wt, base, author)
    if files is None:   # merges cleanly here: GitHub's view was older than the base this run sees
        _base_taken_in(ctx, wt, base, [])
        return
    listed = "\n".join(f"- `{f}`" for f in files)
    findings = (f"The base branch `{ctx.base_branch}` changed the same lines as this ticket, so its pull request can "
                f"no longer be merged. The newest `{ctx.base_branch}` ({base[:7]}) is being merged into this branch "
                "right now (the merge is open: do not commit, do not abort it), and these files hold conflict "
                f"markers:\n\n{listed}")
    if ctx.feedback:
        findings += "\n\nGuidance from the owner of the repository:\n" + ctx.feedback
        ctx.feedback = None

    def back() -> None:
        if gitops.merging(wt):
            gitops.abort_merge(wt)
        gitops.git(wt, "reset", "--quiet", "--hard", before)

    try:
        stations.rework(ctx, wt, "base", findings, escalate=False)
    except BaseException:
        back()
        raise
    ctx.status["rework_rounds"] += 1
    left = gitops.conflict_markers(wt, files)
    if left:
        back()
        raise Stop(f"Der Merge-Konflikt mit `{ctx.base_branch}` ist nach der Nacharbeit nicht aufgelöst: in "
                   f"{', '.join(left)} stehen noch Konfliktmarker. Der Pull Request bleibt, wie er war; bitte den "
                   "Konflikt von Hand lösen.")
    _base_taken_in(ctx, wt, base, files)


def _base_taken_in(ctx: Ctx, wt: Path, base: str, files: list[str]) -> None:
    """The base is merged: commit the merge, move the ticket's base point, say so, and push."""
    if gitops.merging(wt):
        # No path guard here: the merge brings the base's own changes to protected paths. The checks phase restores
        # protected paths against the new base point, which catches what the implementer may have touched.
        gitops.finish_merge(wt, f"merge {ctx.base_branch} into the ticket branch\n\nTicket: #{ctx.ticket}",
                            ctx.cfg["delivery"]["author"])
    if not gitops.git_ok(wt, "merge-base", "--is-ancestor", base, "HEAD"):
        raise Stop(f"Der neue Stand von `{ctx.base_branch}` ({base[:7]}) ist nach der Nacharbeit nicht im Branch.")
    ctx.status["base_sha"] = base_point(ctx, wt) or ctx.status["base_sha"]
    solved = f", Konflikt gelöst in {', '.join(files)}" if files else ""
    ctx.emit("pipeline", "pipeline", outcome="base_merged", detail=f"{ctx.base_ref} {base[:7]}{solved}"[:300])
    ctx.note(f"Der Pull Request hatte einen Merge-Konflikt mit `{ctx.base_branch}`. Die Factory hat den Stand "
             f"{base[:7]} selbst eingearbeitet" + (f" und den Konflikt in {', '.join(files)} gelöst" if files else "")
             + "; Checks, Review und Richter haben danach neu geprüft.")
    _keep_work(ctx, wt)


def _rework(ctx: Ctx, wt: Path, source: str, findings: str, open_findings: int | None = None) -> None:
    """One more round for the implementer. The round limit counts rounds that got nowhere: a review that names
    fewer blocking points than the round before shows the work converging (ck3-battle-engine #59 went 5, 2, 1 and
    was stopped at 1), so that round is free, up to twice the limit."""
    st, cfg = ctx.status, ctx.cfg["pipeline"]
    limit = cfg["max_rework_rounds"]
    converging = open_findings is not None and open_findings < st.get("open_findings", open_findings + 1) \
        and "open_findings" in st
    if open_findings is not None:
        st["review_stalled"] = 0 if converging or "open_findings" not in st else st.get("review_stalled", 0) + 1
        st["open_findings"] = open_findings
    exhausted = st["rework_rounds"] >= 2 * limit or (st["rework_rounds"] >= limit and not converging)
    if source == "review" and (exhausted or st.get("review_stalled", 0) >= 2):
        # Checks still gate: the change is green. More rounds of the same open points cost money and leave the work
        # unseen (ck3-battle-engine #179 and #181 stopped after six rejections each); the owner decides on the PR.
        raise ReviewOpen(f"Nach {st['rework_rounds']} Nacharbeitsrunden ohne Fortschritt verlangt das Review weiter "
                         f"Änderungen ({open_findings} blockierende Punkte). Die Factory liefert die Änderung trotzdem "
                         "als Pull Request; die offenen Punkte stehen darin, gemergt wird nur von Hand.")
    if exhausted:
        raise Stop(f"Nach {st['rework_rounds']} Nacharbeitsrunden ist `{source}` weiter nicht zufrieden:\n\n{findings[:1500]}")
    if st["rework_rounds"] >= limit:
        ctx.note(f"Runde {st['rework_rounds'] + 1} über dem Limit von {limit}: `{source}` meldet weniger offene Punkte "
                 f"({open_findings}) als in der Runde davor.")
    if ctx.feedback:
        findings += ("\n\nGuidance from the owner of the repository, written after the factory stopped. "
                     "Where it differs from the ticket, the guidance wins:\n" + ctx.feedback)
        ctx.feedback = None
    stations.rework(ctx, wt, source, findings, escalate=st["rework_rounds"] >= cfg["escalate_after"])
    st["rework_rounds"] += 1
    _commit(ctx, wt, f"rework after {source}, round {st['rework_rounds']}")


def _checks_and_review(ctx: Ctx, wt: Path) -> None:
    """Checks always gate. The review gates only in gate mode; in advisory mode it is recorded."""
    ticket = contracts.read_json(ctx.run_dir / "ticket.json", "ticket")
    while True:
        ctx.checkpoint("checks")
        _time_check(ctx)
        checks.wait_for_disk(ctx, wt)
        results = checks.run_all(ctx, wt)
        failed = [r for r in results if not r.ok]
        ctx.emit("checks", "verdict", outcome="fail" if failed else "pass",
                 detail=", ".join(r.name for r in failed) or None)
        if not failed:
            warm.keep(ctx.profile, ctx.repo, wt)   # the next ticket on this runner starts from this state
        if failed:
            _rework(ctx, wt, "checks", stations.triage(ctx, wt, failed))
            continue
        evidence.gather(ctx, wt)   # before the review, so the reviewer sees this round's pictures
        floormap.overlay(ctx, wt)  # where on the project's floor plan this change lies (never stops the ticket)
        mode = ctx.mode("review")
        if mode == "off":
            return
        result = stations.review(ctx, wt, ticket)
        ctx.emit("review", "verdict", outcome=result["verdict"], detail=f"{len(result['blocking'])} blocking")
        if result["verdict"] == "changes" and mode == "gate":
            findings = "\n".join(
                f"- {f.get('file', '')}{':' + str(f['line']) if f.get('line') else ''} {f['text']}".replace("-  ", "- ")
                for f in result["blocking"]) or "The reviewer asked for changes without listing blocking points."
            try:
                _rework(ctx, wt, "review", findings, open_findings=len(result["blocking"]))
            except ReviewOpen as open_review:
                ctx.emit("review", "pipeline", outcome="escalated", detail=f"{len(result['blocking'])} blocking")
                ctx.note(str(open_review))
                return
            continue
        return
