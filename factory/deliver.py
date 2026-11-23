"""Delivery: act on the verdict. Opens the pull request or sends the ticket back."""
from __future__ import annotations

from pathlib import Path

from . import contracts, events, evidence, floormap, split, summary
from .context import Ctx

MODES = {"advisory": "beratend", "gate": "Tor", "off": "aus"}


def _plan(ctx: Ctx) -> dict | None:
    path = ctx.run_dir / "plan.md"
    return contracts.read_frontmatter(path, "plan")[0] if path.exists() else None


def assumptions(ticket: dict, plan: dict | None) -> list[str]:
    """What intake and planner decided themselves instead of asking (pipeline.questions: assume)."""
    return list(dict.fromkeys([*(ticket.get("assumptions") or []), *((plan or {}).get("assumptions") or [])]))


def _pr_body(ctx: Ctx, verdict: dict | None) -> str:
    run = ctx.run_dir
    ticket = contracts.read_json(run / "ticket.json", "ticket")
    plan = contracts.read_frontmatter(run / "plan.md", "plan")[0]
    evs = events.read(run)

    review = contracts.read_frontmatter(run / "review.md", "review")[0] if (run / "review.md").exists() else None
    rows = summary.proof_rows(ctx, verdict, review, evs)

    parts = [
        f"Closes #{ctx.ticket}", "", f"**Auftrag:** {ticket['mandate']}", "", "**Abnahmekriterien**",
        *[f"- {a}" for a in ticket["acceptance"]], "",
        f"**Plan** ({len(plan['tasks'])} Tasks): {plan['summary']}",
        *[f"- {t['id']} {t['title']}" for t in plan["tasks"]], "", "**Prüfungen**", "", *rows,
    ]

    def findings(title: str, items: list[str]) -> None:
        if items:
            parts.extend(["", f"**{title}**", *[f"- {i}" for i in items]])

    findings("Annahmen, bitte prüfen", assumptions(ticket, plan))

    if review:
        findings("Review: blockierende Punkte", [
            f"`{f['file']}`{':' + str(f['line']) if f.get('line') else ''} {f['text']}" if f.get("file") else f["text"]
            for f in review["blocking"]])
        findings("Review: unkritische Punkte", [f["text"] for f in review["minor"]])
    if verdict:
        findings("Richter: Fehlerbilder", [f["symptom"] for f in verdict["failures"]])
    findings("Hinweise", ctx.status["notes"])

    parts += evidence.pr_section(ctx)
    plan_of_floor = floormap.section(ctx.run_dir)
    if plan_of_floor:
        parts += [""] + plan_of_floor
    runs = [e for e in evs if e["kind"] == "agent"]
    parts += ["", f"Kosten: {ctx.status['spent_usd']:.2f} $ in {len(runs)} Agent-Läufen, Runde {ctx.status['round']}.",
              "", "Erstellt von der Software Factory. Bitte prüfen und von Hand mergen."]
    return "\n".join(parts)


def deliver(ctx: Ctx, verdict: dict | None) -> str:
    st, host, L = ctx.status, ctx.host, ctx.label
    if st["phase"] != "judge":
        print(f"[factory] nothing to deliver: ticket is in phase {st['phase']}", flush=True)
        return "skipped"
    if verdict is not None:
        contracts.write_json(ctx.run_dir / "verdict.json", verdict, "verdict")
        ctx.spend(float(verdict.get("cost_usd") or 0.0))
        outcome = "none" if verdict["passed"] is None else ("pass" if verdict["passed"] else "fail")
        ctx.emit("judge", "verdict", outcome=outcome, model=verdict.get("model"),
                 cost_usd=verdict.get("cost_usd"), detail=f"{verdict['scenarios_passed']}/{verdict['scenarios_total']}",
                 duration_s=verdict.get("duration_s"), turns=verdict.get("turns"),
                 skills_offered=verdict.get("skills_offered"), skills_used=verdict.get("skills_used"),
                 tools=verdict.get("tools"), runtime=verdict.get("runtime"), tokens=verdict.get("tokens"),
                 context_peak=verdict.get("context_peak"), context_window=verdict.get("context_window"),
                 subscription=verdict.get("subscription"))

    gate = ctx.mode("judge") == "gate"
    if gate and (verdict is None or verdict["passed"] is None):
        # A gate that did not judge must not wave the change through.
        st["stopped"] = "Richter ist ein Tor, hat aber kein Urteil abgegeben"
        host.comment("**Factory angehalten:** Der Richter ist als Tor eingestellt, hat aber kein Urteil abgegeben "
                     f"({(verdict or {}).get('notes') or 'der Richter-Job ist nicht durchgelaufen'}).")
        host.labels(add=[L("needs_human")], remove=[L("running")])
        ctx.save("stopped: no verdict")
        return "failed"
    failed = verdict is not None and verdict["passed"] is False
    if failed and gate:
        symptoms = "\n".join(f"- {f['symptom']}" for f in verdict["failures"])
        if st["round"] < ctx.cfg["pipeline"]["max_judge_rounds"]:
            st["round"] += 1
            st["phase"], st["rework_rounds"] = "rework_judge", 0
            host.comment(f"**Der Richter hat den Stand zurückgeschickt** ({verdict['scenarios_passed']} von "
                         f"{verdict['scenarios_total']} Szenarien). Runde {st['round']} startet.\n\n{symptoms}")
            ctx.save("judge sent the ticket back")
            host.dispatch()
            return "rework"
        st["stopped"] = "Richter nach der letzten Runde nicht zufrieden"
        st["stopped_at"] = host.comment(
            f"**Factory angehalten:** Der Richter ist nach Runde {st['round']} nicht zufrieden.\n\n{symptoms}\n\n"
            f"Das Label `{L('go')}` startet eine weitere Runde; ein Kommentar von dir davor geht als Hinweis an den "
            "Implementierer.")
        host.labels(add=[L("needs_human")], remove=[L("running")])
        ctx.save("stopped after the judge")
        return "failed"
    auto = int(ctx.cfg["pipeline"].get("advisory_judge_rounds") or 0)
    if failed and not gate and st.get("judge_auto_rounds", 0) < auto:
        # An advisory judge does not hold the change back, but what it found is still worth one more try before
        # the owner sees the pull request: up to advisory_judge_rounds rounds, then it is delivered as it stands.
        st["judge_auto_rounds"] = st.get("judge_auto_rounds", 0) + 1
        st["round"] += 1
        st["phase"], st["rework_rounds"] = "rework_judge", 0
        symptoms = "\n".join(f"- {f['symptom']}" for f in verdict["failures"])
        host.comment(f"**Der Richter ist nicht zufrieden** ({verdict['scenarios_passed']} von {verdict['scenarios_total']} "
                     f"Szenarien). Die Factory bessert automatisch nach (Runde {st['judge_auto_rounds']} von {auto}); "
                     f"danach kommt der Pull Request so oder so.\n\n{symptoms}")
        ctx.save("advisory judge sent the ticket back")
        host.dispatch()
        return "rework"

    ticket = contracts.read_json(ctx.run_dir / "ticket.json", "ticket")
    url = host.open_pr(st["branch"], st["base_branch"] or host.default_branch(), ticket["title"], _pr_body(ctx, verdict))
    st["phase"], st["pr"] = "delivered", url
    _start_project_checks(ctx)

    held = ""
    if ctx.cfg["delivery"].get("auto_merge"):
        reasons = merge_blockers(ctx, verdict, ticket) or _freshen(ctx, ticket["title"])
        if not reasons:
            reasons = _merge(ctx, url, verdict)
            if not reasons:
                return "merged"
        ctx.emit("pipeline", "pipeline", outcome="merge_held", detail="; ".join(reasons)[:300])
        held = "\n\nNicht automatisch gemergt, weil:\n" + "\n".join(f"- {r}" for r in reasons)
        if not ctx.cfg["delivery"]["auto_merge_rules"].get("require_judge", True) and _waits_for_ci(reasons):
            held += "\n\nSobald die CI des Projekts grün ist, merged die Factory selbst."

    host.comment(f"**Bereit zur Prüfung:** {url}{held}\n\nVerbraucht: {st['spent_usd']:.2f} $.")
    host.labels(add=[L("review")], remove=[L("running")])
    ctx.emit("pipeline", "pipeline", outcome="delivered", detail=url)
    ctx.save("delivered")
    return "delivered"


def _merge(ctx: Ctx, url: str, verdict: dict | None) -> list[str]:
    """Merge the pull request. Returns why it did not happen; empty means it is merged."""
    st, host, L = ctx.status, ctx.host, ctx.label
    method = (ctx.cfg["delivery"].get("auto_merge_rules") or {}).get("method", "squash")
    error = host.merge_pr(url, st["branch"], method)
    if error is not None:
        return [f"der Merge wurde abgelehnt: {error}"]
    st["phase"] = "merged"
    judged = (f"Richter ({verdict['scenarios_passed']} von {verdict['scenarios_total']} Szenarien)"
              if verdict and verdict.get("passed") else "die CI des Projekts")
    if not summary.post(ctx, "factory"):   # the closing report says it all; without it, the short line
        host.comment(f"**Automatisch gemergt:** {url}\n\nChecks, Review und {judged} haben bestanden. "
                     f"Verbraucht: {st['spent_usd']:.2f} $.")
    host.labels(add=[L("done")], remove=[L("running"), L("review")])
    host.close_issue()  # the closing keyword in the pull request is not honoured for a merge by the job token
    split.release(ctx, host.issue()["body"])
    ctx.emit("pipeline", "pipeline", outcome="merged", detail=url)
    ctx.save("merged automatically")
    return []


def _waits_for_ci(reasons: list[str]) -> bool:
    """A merge GitHub refused only because required checks have not run yet."""
    return len(reasons) == 1 and reasons[0].startswith("der Merge wurde abgelehnt")


def merge_blockers(ctx: Ctx, verdict: dict | None, ticket: dict) -> list[str]:
    """Why this change must not be merged without a human. Empty means: every rule is met.

    The rules look at outcomes, not at modes: a station that only advises still
    has to have said yes.
    """
    rules = ctx.cfg["delivery"].get("auto_merge_rules") or {}
    reasons = []
    if not getattr(ctx.host, "can_merge", False):
        reasons.append("dieser Lauf hat keinen Code-Host, auf dem gemergt werden kann")
    if ctx.status.get("dry_run"):
        reasons.append("es war ein Trockenlauf: kein Modell hat an der Änderung gearbeitet")
    if verdict is not None and verdict["passed"] is False:
        reasons.append("der Richter hat nicht bestanden")
    elif (verdict is None or verdict["passed"] is None) and rules.get("require_judge", True):
        reasons.append("der Richter hat nicht geurteilt")
    elif verdict is not None and verdict["passed"] and verdict["scenarios_total"] < rules.get("min_scenarios", 1):
        reasons.append(f"der Richter hatte nur {verdict['scenarios_total']} Szenarien, verlangt sind mindestens "
                       f"{rules.get('min_scenarios', 1)}")
    review_file = ctx.run_dir / "review.md"
    review = contracts.read_frontmatter(review_file, "review")[0] if review_file.exists() else None
    if review is None or review["verdict"] != "approve":
        reasons.append("das Review hat nicht freigegeben")
    if assumptions(ticket, _plan(ctx)) and not rules.get("allow_assumptions", False):
        reasons.append("die Factory hat Annahmen getroffen, die ein Mensch prüfen soll")
    if ticket.get("sensitive") and not rules.get("allow_sensitive", False):
        reasons.append("das Ticket ist als sensibel markiert")
    limit, lines = rules.get("max_changed_lines"), ctx.status.get("changed_lines")
    if limit and (lines is None or lines > limit):
        reasons.append(f"die Änderung umfasst {lines if lines is not None else 'unbekannt viele'} Zeilen, "
                       f"automatisch gemergt wird bis {limit}")
    if any(e["kind"] == "guard" and str(e.get("detail", "")).startswith("protected paths") for e in events.read(ctx.run_dir)):
        reasons.append("die Änderung hat geschützte Pfade angefasst")
    return reasons


def _freshen(ctx: Ctx, title: str) -> list[str]:
    """Make sure the branch contains the current base branch and is still green.

    Another ticket may have been merged since this one started. Without a merge
    queue, this is what keeps two changes that are each fine from breaking the
    base branch together.
    """
    from . import checks, gitops, pipeline

    st = ctx.status
    wt = pipeline.ensure_worktree(ctx, title)
    base = st["base_branch"]  # a local run compares against the local base branch
    if ctx.host.remote:
        gitops.git_net(ctx.repo, "fetch", "--quiet", "origin",
                       f"+refs/heads/{st['base_branch']}:refs/remotes/origin/{st['base_branch']}")
        base = f"origin/{st['base_branch']}"
    if gitops.git_ok(wt, "merge-base", "--is-ancestor", base, "HEAD"):
        return []
    if not gitops.merge(wt, base, ctx.cfg["delivery"]["author"]):
        return ["der Basis-Branch hat sich geändert und lässt sich nicht ohne Konflikt einarbeiten"]
    st["base_sha"] = gitops.git(wt, "merge-base", "HEAD", base, check=False) or st["base_sha"]
    failed = [r.name for r in checks.run_all(ctx, wt) if not r.ok]
    if failed:
        return ["nach dem Einarbeiten des Basis-Branches schlagen Checks fehl: " + ", ".join(failed)]
    ctx.host.push(wt, st["branch"])
    ctx.note("Vor dem Merge wurde der Basis-Branch eingearbeitet; der Richter hat den Stand davor geprüft.")
    return []


def ci_result(ctx: Ctx, workflow: str, conclusion: str, run_id: str, sha: str) -> str:
    """The project's CI finished on the pull request of a delivered ticket.

    Red sends the ticket back to the implementer, the same way a red check inside the ticket does; the
    workflow of the ticket then runs again on its own runners. This is how work that needs a machine the
    agents do not have (the Unity licence on a self-hosted Mac) stays a gate without the agents waiting
    for that machine: only the pull request waits for it.
    """
    from .agent import redact

    st, host, L = ctx.status, ctx.host, ctx.label
    if st.get("phase") != "delivered":
        print(f"[factory] CI result ignored: ticket is in phase {st.get('phase')}", flush=True)
        return "ignored"
    head = host.branch_head(st["branch"]) if st.get("branch") else None
    if sha and head and sha != head:   # the branch moved on since this run started: its CI will report again
        print(f"[factory] CI result ignored: {sha[:7]} is not the head of {st['branch']} ({head[:7]})", flush=True)
        return "stale"
    url = host.run_url(run_id)
    if conclusion == "success":
        ctx.emit("ci", "verdict", outcome="pass", detail=f"{workflow} {url}")
        ctx.save(f"CI {workflow} passed")
        if ctx.cfg["delivery"].get("auto_merge") and st.get("pr") and host.checks_state(sha or head) == "green":
            return "merged" if _merge_after_ci(ctx) else "pass"
        return "pass"
    if conclusion not in ("failure", "timed_out", "startup_failure"):   # cancelled, skipped, neutral: nothing to fix
        return "ignored"
    ctx.emit("ci", "verdict", outcome="fail", detail=f"{workflow} {url}")
    log = redact(host.failed_log(run_id))
    st["ci_findings"] = (f"The project's CI workflow `{workflow}` failed on the pull request ({url}). "
                         f"The end of the log of its failed steps:\n\n```\n{log or '(no log available)'}\n```")
    st["ci_rounds"] = st.get("ci_rounds", 0) + 1
    st["phase"], st["rework_rounds"] = "rework_ci", 0
    limit = ctx.cfg["pipeline"].get("max_ci_rounds", 3)
    if st["ci_rounds"] > limit:
        st["stopped"] = f"Die CI des Projekts ist nach {limit} Nacharbeitsrunden weiter rot ({workflow})"
        st["stopped_at"] = host.comment(
            f"**Factory angehalten:** Die CI `{workflow}` ist auch nach {limit} Nacharbeitsrunden rot: {url}\n\n"
            f"Das Label `{L('go')}` (oder „Neu starten“ auf dem Board) startet eine weitere Runde; ein Kommentar "
            "von dir davor geht als Hinweis an den Implementierer.")
        host.labels(add=[L("needs_human")], remove=[L("review"), L("running")])
        ctx.save("stopped: CI stays red")
        return "stopped"
    host.comment(f"**Die CI ist rot** (`{workflow}`): {url}\n\nDas Ticket geht zur Nacharbeit zurück "
                 f"(Runde {st['ci_rounds']} von {limit}).")
    host.labels(add=[L("running")], remove=[L("review")])
    ctx.save("CI sent the ticket back")
    host.dispatch()
    return "rework"


def base_conflict(ctx: Ctx, base_sha: str) -> str:
    """GitHub reports the pull request of a delivered ticket as conflicting with its base: another change on the base
    touched the same lines. The orchestrator's sweep sends the ticket back, the same way a red CI does: its next run
    merges the base in, the implementer resolves the conflict (pipeline phase rework_base), and checks, review and
    judge look at the result again before the pull request is updated.

    Once per commit of the base: a conflict that is still there on the same base stays for the owner, so the
    factory does not spend money in a loop on a conflict it cannot resolve."""
    st, host, L = ctx.status, ctx.host, ctx.label
    if st.get("phase") != "delivered":
        return "ignored"
    if st.get("conflict_base") == base_sha:
        return "tried"
    base = st.get("base_branch") or "der Basis"
    st["conflict_base"], st["conflict_rounds"] = base_sha, st.get("conflict_rounds", 0) + 1
    st["phase"], st["rework_rounds"] = "rework_base", 0
    why = f"Merge-Konflikt mit {base} ({base_sha[:7]}): die Factory arbeitet den neuen Stand ein"
    host.comment(f"**Orchestrator:** Der Pull Request {st.get('pr') or ''} hat einen Merge-Konflikt mit `{base}`. "
                 f"Die Factory arbeitet den neuen Stand von `{base}` ({base_sha[:7]}) selbst ein, löst den Konflikt "
                 "und prüft die Änderung danach wieder mit Checks, Review und Richter. Danach wartet der Pull Request "
                 "wieder auf deinen Merge.")
    host.labels(add=[L("running")], remove=[L("review")])
    ctx.emit("pipeline", "pipeline", outcome="auto_restart", detail=why)   # the orchestrator's restart, as on the board
    ctx.save("merge conflict sent the ticket back")
    host.dispatch()
    return "rework"


def _merge_after_ci(ctx: Ctx) -> bool:
    """Every check on the pull request is green: merge it if the rules allow, as deliver would have."""
    verdict_file = ctx.run_dir / "verdict.json"
    verdict = contracts.read_json(verdict_file, "verdict") if verdict_file.exists() else None
    ticket = contracts.read_json(ctx.run_dir / "ticket.json", "ticket")
    reasons = merge_blockers(ctx, verdict, ticket) or _merge(ctx, ctx.status["pr"], verdict)
    if reasons:
        ctx.emit("pipeline", "pipeline", outcome="merge_held", detail="; ".join(reasons)[:300])
        ctx.save("merge held after CI")
    return not reasons


def human_outcome(ctx: Ctx, merged: bool) -> None:
    """What the human did with the pull request: the yardstick for advisory stations."""
    ctx.emit("human", "human", outcome="merged" if merged else "closed")
    if merged:
        # The pull request targets the base branch, not the default branch, so GitHub does not
        # honour its closing keyword: without this the issue stays open under "review".
        ctx.host.labels(add=[ctx.label("done")], remove=[ctx.label("running"), ctx.label("review")])
        summary.post(ctx, "human")
        ctx.host.close_issue()
        split.release(ctx, ctx.host.issue()["body"])
    ctx.save("pull request " + ("merged" if merged else "closed"))


def _start_project_checks(ctx: Ctx) -> None:
    """Make the checks a branch protection requires run on the pull request.

    GitHub either holds the pull request's own workflows for an approval (a pull request opened with the job
    token), or starts none at all. Held runs are released: only they report on the pull request, so only they
    unblock it. Where nothing was held, the factory starts the workflows named in delivery.trigger_workflows on
    the ticket branch itself."""
    branch = ctx.status["branch"]
    sha = ctx.host.branch_head(branch)
    released = ctx.host.approve_waiting_runs(branch, sha) if sha else []
    if released:
        ctx.emit("pipeline", "pipeline", outcome="checks_released", detail=", ".join(sorted(set(released))))
        return
    for entry in ctx.cfg["delivery"].get("trigger_workflows") or []:
        workflow, inputs = (entry, {}) if isinstance(entry, str) else (entry["file"], entry.get("inputs") or {})
        try:
            ctx.host.run_workflow(workflow, ctx.status["branch"], inputs)
            ctx.emit("pipeline", "pipeline", outcome="checks_requested", detail=workflow)
        except Exception as exc:   # the pull request exists; a check that did not start is said, not fatal
            ctx.note(f"Der Workflow {workflow} ließ sich für den Pull Request nicht starten: {str(exc)[:200]}")
