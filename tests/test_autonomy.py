"""More autonomy: assumptions instead of questions, merging on green CI without scenarios, and the orchestrator."""
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


from conftest import sh
from factory import cli, deliver, fake, hosts, orchestrator, stations

real_fake = fake.run


def overrides(product, text):
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n" + text)
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "overrides")


def ticket_ctx(product, n=1):
    return cli.make_ctx(SimpleNamespace(repo=str(product.repo), issue=n, issue_file=str(product.issue), budget=None), local=True)


def comments(product):
    path = product.run_dir / "comments.md"
    return path.read_text() if path.exists() else ""


# ---- assumptions instead of questions ---------------------------------------------

def test_assume_mode_tells_intake_and_planner_to_decide_themselves(product, monkeypatch):
    overrides(product, "  pipeline:\n    questions: assume\n    human_plan_approval_above: { tasks: 0 }\n")
    prompts = {}

    def run(role, values, cwd, out):
        prompts[role] = values
        real_fake(role, values, cwd, out)
        if role == "intake":
            ticket = json.loads(out.read_text())
            out.write_text(json.dumps({**ticket, "assumptions": ["The list is sorted by date, newest first."]}))
    monkeypatch.setattr(fake, "run", run)
    assert cli.main(product.args) == 0
    assert "assumptions" in prompts["intake"]["needs_info_rule"] and "only the owner" in prompts["intake"]["needs_info_rule"]
    assert "decide it yourself" in prompts["planner"]["questions_rule"]
    st = product.status()
    assert st["phase"] == "delivered"                      # a plan over the approval threshold did not wait
    assert any("ohne Freigabe" in n for n in st["notes"])
    assert "Annahmen der Factory" in comments(product)
    body = (product.run_dir / "pr.md").read_text()
    assert "Annahmen, bitte prüfen" in body and "newest first" in body


def test_ask_mode_keeps_the_old_questions():
    ctx = SimpleNamespace(cfg={"pipeline": {}})
    assert stations.policy(ctx) == "ask" and "Ask at most three" in stations.NEEDS_INFO["ask"]


# ---- merging on green CI without scenarios ----------------------------------------

def merging(monkeypatch, merged):
    monkeypatch.setattr(hosts.LocalHost, "can_merge", True, raising=False)
    monkeypatch.setattr(hosts.LocalHost, "merge_pr", lambda self, url, branch, method: merged.append(url) or None, raising=False)


def test_a_dry_run_is_never_merged(product, monkeypatch):
    overrides(product, "  delivery:\n    auto_merge: true\n    auto_merge_rules: { require_judge: false }\n")
    merged = []
    monkeypatch.setenv("FACTORY_DRY_RUN", "1")             # what run-ticket.yml sets for "Trockenlauf"
    assert cli.main(product.args[:-2]) == 0
    assert product.status()["dry_run"] is True
    ctx = ticket_ctx(product)
    ctx.status["pr"] = "https://github.com/me/tiny/pull/5"
    merging(monkeypatch, merged)
    assert deliver.ci_result(ctx, "e2e suite", "success", "9", "") == "pass" and merged == []
    assert any("Trockenlauf" in str(e.get("detail")) for e in product.events() if e.get("outcome") == "merge_held")


def test_green_ci_merges_without_scenarios_when_the_judge_is_not_required(product, monkeypatch):
    overrides(product, "  delivery:\n    auto_merge: true\n    auto_merge_rules: { require_judge: false }\n"
                       "  verify:\n    generated_scenarios: { enabled: false }\n")
    merged = []
    assert cli.main(product.args[:-2]) == 0                # no scenarios: the judge gives no verdict
    st = product.status()
    assert st["phase"] == "delivered" and "nicht automatisch gemergt" in comments(product).lower()
    ctx = ticket_ctx(product)
    ctx.status["pr"] = "https://github.com/me/tiny/pull/5"
    merging(monkeypatch, merged)
    assert deliver.ci_result(ctx, "e2e suite", "success", "9", "") == "merged"
    assert merged == ["https://github.com/me/tiny/pull/5"] and product.status()["phase"] == "merged"
    assert "## Abschlussbericht" in comments(product) and "| CI des Projekts | Tor | grün" in comments(product)


def test_assumptions_and_a_missing_judge_hold_the_merge_by_default(product, monkeypatch):
    monkeypatch.setattr(hosts.LocalHost, "can_merge", True, raising=False)
    assert cli.main(product.args[:-2]) == 0
    ctx = ticket_ctx(product)
    ticket = {"assumptions": ["guessed"], "sensitive": False}
    reasons = deliver.merge_blockers(ctx, None, ticket)
    assert "der Richter hat nicht geurteilt" in reasons
    assert any("Annahmen" in r for r in reasons)
    ctx.cfg["delivery"]["auto_merge_rules"].update(require_judge=False, allow_assumptions=True)
    assert deliver.merge_blockers(ctx, None, ticket) == []
    failed = {"passed": False, "scenarios_total": 3, "scenarios_passed": 1}
    assert "der Richter hat nicht bestanden" in deliver.merge_blockers(ctx, failed, ticket)


# ---- slots and the daily budget ---------------------------------------------------

def test_a_ticket_waits_for_a_free_slot_and_a_finished_one_starts_it(product, monkeypatch):
    overrides(product, "  limits:\n    max_parallel_tickets: 1\n")
    busy = {"factory:running": [{"number": 7, "created_at": "2026-11-17T10:00:00Z"}],
            "factory:queued": [{"number": 1, "created_at": "2026-11-17T10:05:00Z"},
                               {"number": 3, "created_at": "2026-11-17T10:06:00Z"}]}
    monkeypatch.setattr(hosts.LocalHost, "open_issues", lambda self, label: busy.get(label, []))
    assert cli.main(product.args) == 0
    st = product.status()
    assert st["queued"] == "alle 1 Plätze sind belegt" and st["phase"] == "intake"
    assert "Wartet auf einen freien Platz" in comments(product)
    ctx = ticket_ctx(product)
    assert orchestrator.start_queued(ctx) == []           # still full
    busy["factory:running"] = []
    assert orchestrator.start_queued(ctx) == [1]          # one slot: the oldest waiting ticket
    assert json.loads((product.run_dir / "dispatched.jsonl").read_text().splitlines()[-1]) == {"issue": 1}


def test_the_daily_budget_counts_every_ticket_of_today(tmp_path):
    today = datetime.now(timezone.utc)
    for n, (ts, cost) in enumerate([(today, 2.5), (today, 1.0), (today - timedelta(days=1), 9.0)], 1):
        d = tmp_path / str(n)
        d.mkdir()
        (d / "events.jsonl").write_text(json.dumps({"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "cost_usd": cost}) + "\n")
    assert orchestrator.spent_today(tmp_path) == 3.5


def test_no_money_left_today_queues_the_ticket(product, monkeypatch):
    overrides(product, "  limits:\n    daily_budget_usd: 0.5\n")
    monkeypatch.setattr(orchestrator, "spent_today", lambda d: 0.75)
    assert cli.main(product.args) == 0
    assert "Tagesbudget" in product.status()["queued"]


# ---- the sweep ----------------------------------------------------------------------

def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_ticket_without_a_run_counts_as_broken_off():
    run = {"phase": "implement", "run_urls": ["https://github.com/me/x/actions/runs/11"]}
    R = "factory:running"
    assert orchestrator._stuck(run, ago(30), {R}, [], 20, R)
    assert not orchestrator._stuck(run, ago(5), {R}, [], 20, R)                                   # too fresh
    assert not orchestrator._stuck(run, ago(30), {R}, [{"id": "11", "created_at": ago(40)}], 20, R)  # its run is alive
    assert not orchestrator._stuck(run, ago(30), {R}, [{"id": "12", "created_at": ago(1)}], 20, R)   # maybe its new run
    assert not orchestrator._stuck({**run, "phase": "delivered"}, ago(30), {R}, [], 20, R)
    assert not orchestrator._stuck({**run, "waiting": {"reason": "x"}}, ago(30), {R}, [], 20, R)


def test_the_sweep_restarts_a_machinery_stop_twice_then_leaves_it(product, monkeypatch):
    assert cli.main(product.args) == 0
    status = product.status()
    status.update(phase="implement", stopped="unerwarteter Fehler im Steuerskript:\nGitError")
    (product.run_dir / "status.json").write_text(json.dumps(status))
    ctx = ticket_ctx(product)
    orchestrator.sweep(ctx, lambda n: ticket_ctx(product, n))
    assert orchestrator.sweep(ctx, lambda n: ticket_ctx(product, n))["restarted"] == []   # its restart is still fresh
    monkeypatch.setattr(orchestrator, "_age_minutes", lambda ts: 999.0)                  # ... and later it is not
    for _ in range(2):
        done = orchestrator.sweep(ctx, lambda n: ticket_ctx(product, n))
    dispatched = [json.loads(line) for line in (product.run_dir / "dispatched.jsonl").read_text().splitlines()]
    assert dispatched == [{"issue": 1}, {"issue": 1}] and done["left"] == [1]
    assert product.status()["auto_restarts"] == 2
    assert comments(product).count("**Orchestrator:**") == 2


def test_the_sweep_leaves_a_stop_that_was_about_the_work(product):
    assert cli.main(product.args) == 0
    status = product.status()
    status.update(phase="checks", stopped="Nach 3 Nacharbeitsrunden ist `checks` weiter nicht zufrieden")
    (product.run_dir / "status.json").write_text(json.dumps(status))
    done = orchestrator.sweep(ticket_ctx(product), lambda n: ticket_ctx(product, n))
    assert done["restarted"] == [] and not (product.run_dir / "dispatched.jsonl").exists()


def test_the_sweep_catches_up_on_a_red_ci_that_never_reported(product, monkeypatch):
    assert cli.main(product.args) == 0
    status = product.status()
    status["pr"] = "https://github.com/me/tiny/pull/5"
    (product.run_dir / "status.json").write_text(json.dumps(status))
    monkeypatch.setattr(hosts.LocalHost, "pull_request", lambda self, url: {"head": "a" * 40, "conflict": False})
    monkeypatch.setattr(hosts.LocalHost, "checks_state", lambda self, sha: "red")
    monkeypatch.setattr(hosts.LocalHost, "failed_check", lambda self, sha: ("suite", "77"), raising=False)
    monkeypatch.setattr(hosts.LocalHost, "failed_log", lambda self, run_id, limit=12000, wait_s=300: "error CS1002")
    done = orchestrator.sweep(ticket_ctx(product), lambda n: ticket_ctx(product, n))
    assert done["ci"] == [(1, "rework")] and product.status()["phase"] == "rework_ci"
    status = product.status()
    status["phase"] = "delivered"                       # as if the rework had delivered the same head again
    (product.run_dir / "status.json").write_text(json.dumps(status))
    assert orchestrator.sweep(ticket_ctx(product), lambda n: ticket_ctx(product, n))["ci"] == []   # seen already


# ---- a delivered pull request in conflict with its base ------------------------------

def delivered_with_a_pr(product, monkeypatch, base="b" * 40):
    assert cli.main(product.args) == 0
    status = product.status()
    assert status["phase"] == "delivered"
    status["pr"] = "https://github.com/me/tiny/pull/5"
    (product.run_dir / "status.json").write_text(json.dumps(status))
    heads = {"base": base}
    monkeypatch.setattr(hosts.LocalHost, "pull_request", lambda self, url: {"head": "a" * 40, "conflict": True})
    monkeypatch.setattr(hosts.LocalHost, "branch_head", lambda self, branch: heads["base"])
    monkeypatch.setattr(hosts.LocalHost, "checks_state", lambda self, sha: "pending")
    return heads


def dispatches(product):
    path = product.run_dir / "dispatched.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_the_sweep_sends_a_conflicted_pull_request_back_once_per_base(product, monkeypatch):
    heads = delivered_with_a_pr(product, monkeypatch)
    ctx = ticket_ctx(product)
    done = orchestrator.sweep(ctx, lambda n: ticket_ctx(product, n))
    st = product.status()
    assert done["conflicts"] == [1] and st["phase"] == "rework_base" and st["conflict_base"] == "b" * 40
    assert dispatches(product) == [{"issue": 1}]
    assert "Merge-Konflikt mit `main`" in comments(product)
    assert product.events()[-1]["outcome"] == "auto_restart"            # the orchestrator's restart, on the board
    assert done["decisions"][0]["action"] == "restarted" and "Merge-Konflikt" in done["decisions"][0]["why"]

    st["phase"] = "delivered"                   # the round delivered, and GitHub still sees a conflict on the same base
    (product.run_dir / "status.json").write_text(json.dumps(st))
    monkeypatch.setattr(orchestrator, "_age_minutes", lambda ts: 999.0)
    assert orchestrator.sweep(ctx, lambda n: ticket_ctx(product, n))["conflicts"] == []   # no loop: it stays for you
    assert len(dispatches(product)) == 1

    heads["base"] = "c" * 40                    # the base moved on: worth one more try
    assert orchestrator.sweep(ctx, lambda n: ticket_ctx(product, n))["conflicts"] == [1]
    assert product.status()["conflict_rounds"] == 2


def test_a_conflicted_pull_request_waits_for_a_free_slot(product, monkeypatch):
    overrides(product, "  limits:\n    max_parallel_tickets: 1\n")
    delivered_with_a_pr(product, monkeypatch)
    busy = {"factory:running": [{"number": 7, "created_at": "2026-11-17T10:00:00Z"}]}
    monkeypatch.setattr(hosts.LocalHost, "open_issues", lambda self, label: busy.get(label, []))
    done = orchestrator.sweep(ticket_ctx(product), lambda n: ticket_ctx(product, n))
    assert done["conflicts"] == [] and dispatches(product) == []
    assert done["decisions"] == [{"ticket": 1, "action": "left",
                                  "why": "Merge-Konflikt mit main, aber alle 1 Plätze sind belegt"}]
    assert product.status()["phase"] == "delivered" and "conflict_base" not in product.status()
    busy["factory:running"] = []                # the slot is free at the next sweep
    assert orchestrator.sweep(ticket_ctx(product), lambda n: ticket_ctx(product, n))["conflicts"] == [1]


def test_a_conflicted_pull_request_waits_when_the_money_for_today_is_spent(product, monkeypatch):
    overrides(product, "  limits:\n    daily_budget_usd: 0.5\n")
    delivered_with_a_pr(product, monkeypatch)
    monkeypatch.setattr(orchestrator, "spent_today", lambda d: 0.75)
    done = orchestrator.sweep(ticket_ctx(product), lambda n: ticket_ctx(product, n))
    assert done["conflicts"] == [] and "Tagesbudget" in done["decisions"][0]["why"]


def conflict_on_the_base(product):
    """The base changed the same file as the ticket: GitHub would call the pull request conflicting."""
    (product.repo / "FACTORY_DRYRUN.md").write_text("# Written on the base\n\nsomething else entirely\n")
    sh(product.repo, "git", "add", "-A")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base change")
    return sh(product.repo, "git", "rev-parse", "HEAD")


def resolving_implementer(monkeypatch, resolve=True):
    def run(role, values, cwd, out):
        marker = Path(cwd) / "FACTORY_DRYRUN.md"
        if role == "implementer" and values.get("source") == "base":
            assert "FACTORY_DRYRUN.md" in values["findings"] and "conflict" in values["source_hint"]
            if resolve:
                marker.write_text("# Written on the base\n\nsomething else entirely\n\n- pass 1\n")
            return None
        return real_fake(role, values, cwd, out)
    monkeypatch.setattr(fake, "run", run)


def test_the_next_run_resolves_the_conflict_and_delivers_again(product, monkeypatch):
    delivered_with_a_pr(product, monkeypatch)
    base = conflict_on_the_base(product)
    assert deliver.base_conflict(ticket_ctx(product), base) == "rework"
    resolving_implementer(monkeypatch)
    assert cli.main(product.args) == 0
    st = product.status()
    assert st["phase"] == "delivered" and not st.get("stopped")
    branch = st["branch"]
    assert sh(product.repo, "git", "merge-base", "--is-ancestor", base, branch) == ""    # the base is in the branch
    assert "<<<<<<<" not in sh(product.repo, "git", "show", f"{branch}:FACTORY_DRYRUN.md")
    merged = [e for e in product.events() if e.get("outcome") == "base_merged"]
    assert merged and "Konflikt gelöst in FACTORY_DRYRUN.md" in merged[-1]["detail"]
    outcomes = [e.get("outcome") for e in product.events()]
    assert outcomes.index("base_merged") < len(outcomes) - 1 - outcomes[::-1].index("pass")   # checks ran after it
    assert any("Merge-Konflikt" in n for n in st["notes"])                 # in the PR and the closing report
    assert "Bereit zur Prüfung" in comments(product).rsplit("Merge-Konflikt", 1)[-1]


def test_a_conflict_the_implementer_leaves_stops_with_the_branch_as_it_was(product, monkeypatch):
    delivered_with_a_pr(product, monkeypatch)
    head = sh(product.repo, "git", "rev-parse", product.status()["branch"])
    base = conflict_on_the_base(product)
    deliver.base_conflict(ticket_ctx(product), base)
    resolving_implementer(monkeypatch, resolve=False)
    assert cli.main(product.args) == 0
    st = product.status()
    assert "Konfliktmarker" in (st.get("stopped") or "")
    assert sh(product.repo, "git", "rev-parse", st["branch"]) == head


def test_checks_count_once_by_name(monkeypatch):
    host = hosts.GitHubHost.__new__(hosts.GitHubHost)
    runs = [{"name": "suite", "status": "completed", "conclusion": "cancelled"},
            {"name": "suite", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "success"}]
    answers = {"check-runs": {"check_runs": runs}, "status": {"total_count": 0, "state": "pending"}}
    monkeypatch.setattr(hosts.GitHubHost, "_api", lambda self, method, path, body=None, check=True:
                        answers["check-runs" if "check-runs" in path else "status"])
    assert host.checks_state("a" * 40) == "green"         # the cancelled duplicate does not hold it back
    runs.append({"name": "build", "status": "in_progress", "conclusion": None})
    assert host.checks_state("a" * 40) == "pending"
    runs.append({"name": "unity", "status": "completed", "conclusion": "failure"})
    assert host.checks_state("a" * 40) == "red"


def test_the_orchestrate_command_starts_and_sweeps(product, monkeypatch, capsys):
    from factory import state
    assert cli.main(product.args) == 0
    monkeypatch.setattr(cli, "GitHubHost", lambda repo, ticket: hosts.LocalHost(product.issue, ticket, product.run_dir))
    monkeypatch.setattr(cli, "BranchState", lambda repo, branch, workdir, author: state.LocalState(product.work / "state"))
    monkeypatch.setattr(cli, "_base", lambda repo, local: ("HEAD", "main"))
    assert cli.main(["orchestrate", "--repo", str(product.repo)]) == 0
    assert "[factory] restarted=" in capsys.readouterr().out


def test_the_sweep_writes_down_what_it_did_and_why(product, monkeypatch):
    """The board shows the orchestrator's decisions: each run leaves one record on the state branch."""
    assert cli.main(product.args) == 0
    status = product.status()
    status.update(phase="implement", stopped="unerwarteter Fehler im Steuerskript:\nGitError")
    (product.run_dir / "status.json").write_text(json.dumps(status))
    ctx = ticket_ctx(product)
    done = orchestrator.sweep(ctx, lambda n: ticket_ctx(product, n))
    assert done["decisions"] == [{"ticket": 1, "action": "restarted",
                                  "why": "angehalten durch einen Fehler der Maschinerie (Neustart 1 von 2)"}]
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    orchestrator.record(ctx, orchestrator.trigger(), done["decisions"], done["checked"], prune=True)
    folder = Path(ctx.store.tickets_dir()).parent / "orchestrator" / "sweeps"
    entry = json.loads(next(folder.glob("*.json")).read_text())
    assert entry["trigger"] == "Zeitplan von GitHub" and entry["checked"] == 1
    assert entry["decisions"][0]["action"] == "restarted" and "tokens_today" in entry["capacity"]


def test_only_the_newest_records_are_kept(product):
    assert cli.main(product.args) == 0
    ctx = ticket_ctx(product)
    folder = Path(ctx.store.tickets_dir()).parent / "orchestrator" / "sweeps"
    folder.mkdir(parents=True)
    for i in range(orchestrator.KEEP + 5):
        (folder / f"2026010{i:04d}.json").write_text("{}")
    orchestrator.record(ctx, "test", [], prune=True)
    assert len(list(folder.glob("*.json"))) == orchestrator.KEEP
