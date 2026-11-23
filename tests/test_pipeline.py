"""The controller, run end to end with canned station outputs (no model calls)."""
import json
from pathlib import Path

import pytest
import yaml

from conftest import sh
from factory import cli, fake, judge
from factory.config import ROOT

real_fake = fake.run


def branch_files(p):
    return sh(p.repo, "git", "ls-tree", "-r", "--name-only", p.status()["branch"]).splitlines()


def test_dry_run_delivers(product):
    assert cli.main(product.args) == 0
    st = product.status()
    assert st["phase"] == "delivered" and st["branch"] == "factory/1-add-a-thing"
    assert "FACTORY_DRYRUN.md" in branch_files(product)
    assert sh(product.repo, "git", "rev-parse", "--abbrev-ref", "HEAD") == "main"   # the checkout stays untouched
    verdict = json.loads((product.run_dir / "verdict.json").read_text())
    assert verdict["scenarios_total"] == 2 and verdict["passed"] is True
    body = (product.run_dir / "pr.md").read_text()
    assert "Closes #1" in body and "2 von 2 Szenarien" in body
    stations = [e["station"] for e in product.events() if e["kind"] == "agent"]
    assert stations == ["intake", "planner", "plan_check", "implementer", "reviewer"]
    assert cli.main(product.args) == 0 and product.status()["phase"] == "delivered"   # running again changes nothing


def no_generated_scenarios(product):
    profile = product.repo / ".factory" / "profile.yaml"
    text = profile.read_text()
    block = "  verify:\n    generated_scenarios: { enabled: false }\n"
    profile.write_text(text + block if "overrides:" in text else text + "overrides:\n" + block)
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "no generated")


def test_no_scenarios_means_no_verdict_but_still_a_pull_request(product):
    no_generated_scenarios(product)
    assert cli.main(product.args[:-2]) == 0
    verdict = json.loads((product.run_dir / "verdict.json").read_text())
    assert verdict["passed"] is None
    assert "kein Urteil" in (product.run_dir / "pr.md").read_text()


def test_protected_paths_are_reverted(product, monkeypatch):
    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "implementer":
            (Path(cwd) / ".github" / "workflows").mkdir(parents=True)
            (Path(cwd) / ".github" / "workflows" / "evil.yml").write_text("on: push\n")
            (Path(cwd) / ".factory" / "profile.yaml").write_text("schema: profile/1\nname: x\nchecks: [{name: a, run: 'true'}]\n")
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    files = branch_files(product)
    assert ".github/workflows/evil.yml" not in files and "FACTORY_DRYRUN.md" in files
    profile = sh(product.repo, "git", "show", f"{product.status()['branch']}:.factory/profile.yaml")
    assert yaml.safe_load(profile)["name"] == "tiny"
    assert any("geschützten Pfaden" in n for n in product.status()["notes"])


def test_read_only_station_cannot_change_the_tree(product, monkeypatch):
    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "reviewer":
            (Path(cwd) / "README.md").write_text("# rewritten by the reviewer\n")
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    assert sh(product.repo, "git", "show", f"{product.status()['branch']}:README.md") == "# tiny"
    assert any(e["kind"] == "guard" and e["station"] == "reviewer" for e in product.events())


def test_failing_check_goes_back_to_the_implementer(product, monkeypatch):
    calls = {"n": 0}

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "implementer":
            calls["n"] += 1
            target = Path(cwd) / "NOTES.md"
            target.write_text("BROKEN\n" if calls["n"] == 1 else "fine\n")
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    st = product.status()
    assert st["phase"] == "delivered" and st["rework_rounds"] == 1
    assert [e["station"] for e in product.events() if e["kind"] == "agent"] == [
        "intake", "planner", "plan_check", "implementer", "triage", "rework", "reviewer"]


def test_checks_that_never_pass_stop_the_ticket(product, monkeypatch):
    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "implementer":
            (Path(cwd) / "NOTES.md").write_text("BROKEN\n")
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    st = product.status()
    assert st["phase"] == "checks" and "Nacharbeitsrunden" in st["stopped"] and st["rework_rounds"] == 3
    reworks = [e for e in product.events() if e["station"] == "rework"]
    assert [e["model"] for e in reworks] == ["fake"] * 3
    assert "Factory angehalten" in (product.run_dir / "comments.md").read_text()


def test_question_parks_the_ticket_until_the_owner_answers(product, monkeypatch):
    def run(role, values, cwd, out):
        if role == "intake" and "blue" not in str(values["owner_comments"]):
            out.write_text(json.dumps({"schema": "ticket/1", "id": 1, "title": "t", "status": "needs_info",
                                       "mandate": "", "acceptance": [], "size": "S", "sensitive": False,
                                       "questions": ["Which colour?"]}))
        else:
            real_fake(role, values, cwd, out)
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    assert product.status()["waiting"]["reason"] == "intake_questions"
    cli.main(product.args)                                   # nothing new from the owner: still waiting
    assert product.status()["phase"] == "intake"
    cli.main(product.args + ["--answer", "blue"])
    assert product.status()["phase"] == "delivered"


def gate(product, station):
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + f"overrides:\n  pipeline:\n    modes: {{ {station}: gate }}\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "gate")


def test_review_in_gate_mode_sends_back(product, monkeypatch):
    gate(product, "review")
    seen = {"n": 0}

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "reviewer":
            seen["n"] += 1
            if seen["n"] == 1:
                out.write_text("---\nschema: review/1\nverdict: changes\nblocking:\n  - file: a.py\n    line: 3\n"
                               "    text: wrong result for empty input\nminor: []\n---\n")
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    st = product.status()
    assert st["phase"] == "delivered" and st["rework_rounds"] == 1 and seen["n"] == 2


def test_judge_in_gate_mode_sends_back_without_scenario_text(product, monkeypatch):
    gate(product, "judge")

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "judge":
            manifest = json.loads(Path(values["manifest_path"]).read_text())
            out.write_text(json.dumps({"schema": "judge-result/1", "results": [
                {"id": manifest[0]["id"], "satisfied": False,
                 "symptom": "It says: A user adds a thing and sees it in the list afterwards."},
                {"id": manifest[1]["id"], "satisfied": True}]}))
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    st = product.status()
    assert st["phase"] == "rework_judge" and st["round"] == 2
    verdict = json.loads((product.run_dir / "verdict.json").read_text())
    assert verdict["passed"] is False and verdict["failures"][0]["symptom"] == judge.WITHHELD
    assert "sees it in the list" not in (product.run_dir / "comments.md").read_text()

    monkeypatch.setattr(fake, "run", real_fake)             # round two: the judge is satisfied
    cli.main(product.args)
    assert product.status()["phase"] == "delivered"
    assert [e["station"] for e in product.events() if e["kind"] == "agent"][-2:] == ["rework", "reviewer"]


def test_budget_stops_the_ticket(product, monkeypatch):
    from factory import stations
    from factory.agent import AgentResult

    monkeypatch.setenv("FACTORY_FAKE_AGENT", "0")

    def costly(cfg, profile, role, prompt, cwd, **kw):
        real_fake(role, {"issue_number": 1, "issue_title": "t", "ticket": 1}, cwd, product.run_dir / {
            "intake": "ticket.json", "planner": "plan.md"}.get(role, "x"))
        return AgentResult(True, cost_usd=0.6, model=kw["model"])
    monkeypatch.setattr(stations, "run_agent", costly)
    cli.main(product.args + ["--budget", "1"])
    st = product.status()
    # intake costs 0.60 $; the 0.40 $ left cannot pay a quarter of a planner run (3 $), so the planner never starts
    assert st["stopped"] == "Budget des Tickets aufgebraucht" and st["spent_usd"] == pytest.approx(0.6)


def test_safe_symptom():
    scenario = "A user removes entry five from a list of two and expects an error message and exit code two."
    assert judge.safe_symptom("`todo remove 5` exits 0 and prints nothing", scenario).startswith("`todo remove 5`")
    assert judge.safe_symptom("user removes entry five from a list of two and expects an error", scenario) == judge.WITHHELD
    assert judge.safe_symptom("", scenario) == "ohne Beschreibung"


def test_route_reads_capabilities(product, capsys):
    assert cli.main(["route", "--repo", str(product.repo)]) == 0
    out = capsys.readouterr().out
    assert 'work_runner=["ubuntu-latest"]' in out and "work_sandboxed=1" in out
    assert "work_install=@anthropic-ai/claude-code@" in out and "judge_install=@anthropic-ai/claude-code@" in out


def test_prompts_render_with_the_values_the_stations_pass():
    from factory.stations import _PLACEHOLDER
    known = {
        "intake.md": {"issue_number", "issue_title", "issue_body", "owner_comments", "out", "needs_info_rule"},
        "planner.md": {"ticket_path", "out", "feedback_block", "ticket", "questions_rule"},
        "plan-check.md": {"ticket_path", "plan_path", "size", "out"},
        "implementer.md": {"ticket_path", "plan_path", "task_id", "task_title", "task_goal", "task_acceptance",
                           "task_files", "checks", "notes_path"},
        "rework.md": {"ticket_path", "plan_path", "source", "findings", "source_hint", "checks", "notes_path"},
        "triage.md": {"failed", "out"},
        "reviewer.md": {"ticket_path", "plan_path", "base", "out", "sensitive_block", "earlier_review", "evidence_note"},
        "judge.md": {"manifest_path", "out", "judge_notes", "checks_passed"},
        "scenario-writer.md": {"issue_path", "out", "count"},
    }
    for name, values in known.items():
        used = set(_PLACEHOLDER.findall((ROOT / "stations" / name).read_text()))
        assert used <= values, f"{name} uses unknown placeholders {used - values}"


def test_judge_gate_without_verdict_does_not_deliver(product):
    gate(product, "judge")
    no_generated_scenarios(product)
    cli.main(product.args[:-2])                              # no scenarios given
    st = product.status()
    assert st["phase"] == "judge" and "kein Urteil" in st["stopped"]
    assert not (product.run_dir / "pr.md").exists()


def test_branch_names_survive_umlauts():
    from factory.pipeline import _slug
    assert _slug("Einträge löschen mit `todo remove N`") == "eintraege-loeschen-mit-todo-remove-n"
    assert _slug("Größe übernehmen, café") == "groesse-uebernehmen-cafe"
    assert _slug("???") == "ticket"


def test_board_sorts_tickets_into_columns(product):
    from factory import board
    cli.main(product.args)
    cards = board.local_cards(product.work / "state")
    assert [(c["number"], c["column"], c["verdict"]["passed"]) for c in cards] == [(1, "review", True)]
    status = product.run_dir / "status.json"
    st = json.loads(status.read_text())
    st.update(phase="plan", waiting={"reason": "plan_approval", "since": "2026-01-01T00:00:00Z"})
    status.write_text(json.dumps(st))
    assert board.local_cards(product.work / "state")[0]["column"] == "waiting"
    st.update(waiting=None, stopped="Budget des Tickets aufgebraucht")
    status.write_text(json.dumps(st))
    waiting = board.local_cards(product.work / "state")[0]
    assert waiting["column"] == "stopped" and "Budget" in waiting["detail"]


# ---- dark mode: automatic merge ---------------------------------------------

def dark(product, monkeypatch, extra=""):
    """Switch the product to dark mode and give the local host the ability to merge."""
    from factory import hosts
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n  delivery:\n    auto_merge: true\n"
                       "    auto_merge_rules: { min_scenarios: 2 }\n" + extra)
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "dark")
    merged = []
    monkeypatch.setattr(hosts.LocalHost, "can_merge", True)
    monkeypatch.setattr(hosts.LocalHost, "merge_pr", lambda self, url, branch, method: merged.append((branch, method)),
                        raising=False)
    return merged


def test_dark_merges_when_every_station_agrees(product, monkeypatch):
    merged = dark(product, monkeypatch)
    cli.main(product.args)
    assert product.status()["phase"] == "merged"
    assert merged == [("factory/1-add-a-thing", "squash")]
    said = (product.run_dir / "comments.md").read_text()
    assert "## Abschlussbericht" in said and "von der Factory automatisch gemergt" in said
    assert cli.main(product.args) == 0 and len(merged) == 1          # a merged ticket is not merged twice


def test_dark_holds_back_when_the_review_objects(product, monkeypatch):
    merged = dark(product, monkeypatch)

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "reviewer":
            out.write_text("---\nschema: review/1\nverdict: changes\nblocking:\n  - text: wrong for empty input\nminor: []\n---\n")
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)                                              # review only advises, so no rework
    assert product.status()["phase"] == "delivered" and merged == []
    assert "das Review hat nicht freigegeben" in (product.run_dir / "comments.md").read_text()


@pytest.mark.parametrize("extra, change, reason", [
    ("    auto_merge_rules: { min_scenarios: 3 }\n", None, "nur 2 Szenarien"),
    ("    auto_merge_rules: { min_scenarios: 2, max_changed_lines: 1 }\n", None, "automatisch gemergt wird bis 1"),
    ("", "sensitive", "sensibel"),
    ("", "protected", "geschützte Pfade"),
    ("", "judge_fails", "Richter hat nicht bestanden"),
])
def test_dark_guard_rails(product, monkeypatch, extra, change, reason):
    merged = dark(product, monkeypatch, extra.replace("auto_merge_rules", "auto_merge_rules") if not extra else "")
    if extra:   # replace the rules written by dark()
        profile = product.repo / ".factory" / "profile.yaml"
        profile.write_text(profile.read_text().replace("    auto_merge_rules: { min_scenarios: 2 }\n", extra))
        sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "rules")

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "intake" and change == "sensitive":
            out.write_text(out.read_text().replace('"sensitive": false', '"sensitive": true'))
        if role == "implementer" and change == "protected":
            (Path(cwd) / ".factory" / "profile.yaml").write_text("schema: profile/1\nname: x\nchecks: [{name: a, run: 'true'}]\n")
        if role == "judge" and change == "judge_fails":
            manifest = json.loads(Path(values["manifest_path"]).read_text())
            out.write_text(json.dumps({"schema": "judge-result/1", "results": [
                {"id": m["id"], "satisfied": False, "symptom": "exits with code 1"} for m in manifest]}))
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    assert product.status()["phase"] == "delivered" and merged == []
    assert reason in (product.run_dir / "comments.md").read_text()


def commit_on_main(product, name, text):
    (product.repo / name).write_text(text)
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", f"other ticket: {name}")


def test_dark_takes_in_a_base_branch_that_moved(product, monkeypatch):
    merged = dark(product, monkeypatch)

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "reviewer":                                          # another ticket lands meanwhile
            commit_on_main(product, "OTHER.md", "from another ticket\n")
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    st = product.status()
    assert st["phase"] == "merged" and len(merged) == 1
    assert "OTHER.md" in branch_files(product) and "FACTORY_DRYRUN.md" in branch_files(product)
    assert any("Basis-Branch eingearbeitet" in n for n in st["notes"])


def test_dark_does_not_merge_over_a_conflict_or_a_red_check(product, monkeypatch):
    merged = dark(product, monkeypatch)

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "reviewer":
            commit_on_main(product, "FACTORY_DRYRUN.md", "someone else wrote this file\n")
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    assert product.status()["phase"] == "delivered" and merged == []
    assert "nicht ohne Konflikt" in (product.run_dir / "comments.md").read_text()


def test_dark_does_not_merge_when_the_combined_state_is_red(product, monkeypatch):
    merged = dark(product, monkeypatch)

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "reviewer":
            commit_on_main(product, "OTHER.md", "BROKEN\n")           # green alone, red together
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    assert product.status()["phase"] == "delivered" and merged == []
    assert "schlagen Checks fehl: marker" in (product.run_dir / "comments.md").read_text()


def test_running_station_is_published_while_it_runs(product, monkeypatch):
    seen = []

    def run(role, values, cwd, out):
        seen.append((role, product.status()["current"]))
        real_fake(role, values, cwd, out)
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    assert seen[:5] == [("intake", "intake"), ("planner", "planner"), ("plan_check", "plan_check"),
                        ("implementer", "implementer"), ("reviewer", "reviewer")]
    assert product.status()["current"] is None


def test_board_shows_the_workflow_of_a_ticket(product):
    from factory import board
    cli.main(product.args)
    flow = board.local_cards(product.work / "state")[0]["workflow"]
    assert [s["title"] for s in flow["stations"]] == [
        "Eingang", "Planer", "Plan-Prüfung", "Implementierer", "Checks", "Review", "Richter", "Auslieferung", "CI des Projekts"]
    assert {s["state"] for s in flow["stations"][:-1]} == {"done"}
    assert flow["stations"][-1]["state"] == "skipped"   # a local run has no pull request, so no CI
    assert flow["stations"][6]["summary"] == "2 von 2 Szenarien, bestanden"
    assert flow["tasks"] == [{"id": "T1", "title": "Leave a dry run marker", "done": True}]

    status = product.run_dir / "status.json"
    st = json.loads(status.read_text())
    st.update(phase="checks", current="reviewer")
    status.write_text(json.dumps(st))
    card = board.local_cards(product.work / "state")[0]
    assert [s["state"] for s in card["workflow"]["stations"]] == ["done"] * 5 + ["active", "pending", "pending", "skipped"]
    assert card["running"] is True


def test_owner_guidance_restarts_a_ticket_the_judge_stopped(product, monkeypatch):
    gate(product, "judge")
    seen = []

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "implementer":
            seen.append(str(values.get("findings", "")))
        if role == "judge" and "keep the message short" not in " ".join(seen):
            manifest = json.loads(Path(values["manifest_path"]).read_text())
            out.write_text(json.dumps({"schema": "judge-result/1", "results": [
                {"id": m["id"], "satisfied": False, "symptom": "prints 6000 characters to stderr"} for m in manifest]}))
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)                                   # round 1: judge sends back
    cli.main(product.args)                                   # round 2: judge still unsatisfied, factory stops
    st = product.status()
    assert st["phase"] == "judge" and st["round"] == 2 and "Richter" in st["stopped"]
    cli.main(product.args + ["--answer", "keep the message short"])
    st = product.status()
    assert st["phase"] == "delivered" and st["round"] == 3 and st["stopped"] is None
    assert "Where it differs from the ticket, the guidance wins" in seen[-1] and "keep the message short" in seen[-1]


def test_board_groups_sessions_by_station(tmp_path):
    from factory import board
    root = tmp_path / "state" / "tickets" / "4"
    (root / "logs").mkdir(parents=True)
    (root / "sessions").mkdir()
    (root / "checks").mkdir()
    (root / "plan.md").write_text("---\nschema: plan/1\n---\n")
    (root / "notes-T1.md").write_text("changed cli.py")
    (root / "checks" / "tests.log").write_text("$ pytest\nOK")
    (root / "logs" / "01-planner.md").write_text("model: claude-fable-5-2\ncost_usd: 0.5000\nduration_s: 60\nstate: ok\n\nPlan is written.\n")
    (root / "logs" / "02-implementer.md").write_text("model: claude-sonnet-5-5\ncost_usd: 0.1000\nduration_s: 20\nstate: ok\n\nDone.\n")
    (root / "logs" / "03-rework.md").write_text("model: claude-sonnet-5-5\ncost_usd: 0.1000\nduration_s: 20\nstate: ok\n\nFixed.\n")
    (root / "sessions" / "03-rework.jsonl").write_text(
        json.dumps({"ts": "2026-11-16T10:00:00Z", "type": "prompt", "text": "Fix it."}) + "\n"
        + json.dumps({"ts": "2026-11-16T10:00:05Z", "type": "tool", "tool": "Bash", "text": "pytest -q", "input": "{}"}) + "\n")
    stations = board.sessions(*board.local_ticket_files(tmp_path / "state", 4))
    assert [r["final"] for r in stations["planner"]["runs"]] == ["Plan is written."]
    assert stations["planner"]["runs"][0]["meta"] == {"model": "Fable 5.2", "cost": 0.5, "seconds": 60.0, "state": "ok"}
    assert stations["planner"]["outputs"][0]["name"] == "plan.md"
    runs = stations["implementer"]["runs"]
    assert [(r["seq"], r["label"], len(r["entries"])) for r in runs] == [(2, "", 0), (3, "Nacharbeit", 2)]
    assert [o["name"] for o in stations["implementer"]["outputs"]] == ["notes-T1.md"]
    # the logs of the checks are shown with each check, not as files of the station
    assert stations["checks"]["outputs"] == []
    assert [(c["name"], c["log"].startswith("$ ")) for c in stations["checks"]["checks"]] == [("tests", True)]
    assert stations["judge"]["runs"] == [] and "Szenarien" in stations["judge"]["note"]


def test_project_checks_are_started_on_the_ticket_branch(product):
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n  delivery:\n    trigger_workflows:\n"
                       "      - lint.yml\n      - { file: ci.yml, inputs: { tier: fast } }\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "project checks")
    assert cli.main(product.args) == 0
    started = [json.loads(line) for line in (product.run_dir / "workflows.jsonl").read_text().splitlines()]
    branch = product.status()["branch"]
    assert started == [{"workflow": "lint.yml", "ref": branch, "inputs": {}},
                       {"workflow": "ci.yml", "ref": branch, "inputs": {"tier": "fast"}}]
    assert [e["detail"] for e in product.events() if e.get("outcome") == "checks_requested"] == ["lint.yml", "ci.yml"]


def test_ci_of_a_pull_request_becomes_the_last_station():
    from factory import board
    pr = {"pr_url": "https://github.com/me/x/pull/3"}
    run = lambda name, status, conclusion, wf="ci": {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
                                                    "detailsUrl": f"https://github.com/me/x/actions/runs/{name}",
                                                    "startedAt": "2026-11-16T20:00:00Z", "completedAt": "2026-11-16T20:03:00Z",
                                                    "checkSuite": {"workflowRun": {"workflow": {"name": wf}}}}
    checks = [board._check(c) for c in (run("lint", "COMPLETED", "SUCCESS", "lint"), run("suite", "IN_PROGRESS", None),
                                        run("ticket", "COMPLETED", "SKIPPED", "factory"))]
    station = board.ci_station(pr, {"state": "pending", "checks": checks, "commit": "abc1234"})
    assert station["id"] == "ci" and station["state"] == "active"
    assert station["summary"] == "1 von 2 grün · 1 laufen"   # a skipped run does not count
    assert checks[0]["seconds"] == 180 and checks[0]["url"].endswith("/lint")
    red = board.ci_station(pr, {"state": "failure", "checks": [board._check(run("suite", "COMPLETED", "FAILURE"))], "commit": ""})
    assert red["state"] == "failed" and "1 rot" in red["summary"]
    assert board.ci_station({"pr_url": None}, None)["state"] == "pending"
    assert board.ci_station(pr, {"state": None, "checks": [], "commit": ""})["state"] == "skipped"


def _ticket_ctx(product):
    from types import SimpleNamespace
    return cli.make_ctx(SimpleNamespace(repo=str(product.repo), issue=1, issue_file=str(product.issue), budget=None), local=True)


def test_red_ci_on_the_pull_request_sends_the_ticket_back(product, monkeypatch):
    from factory import deliver, hosts
    assert cli.main(product.args) == 0 and product.status()["phase"] == "delivered"
    dispatched, seen = [], []
    monkeypatch.setattr(hosts.LocalHost, "failed_log", lambda self, run_id, limit=12000, wait_s=300: "Assets/Battle/Army.cs(12,5): error CS1002: ; expected")
    monkeypatch.setattr(hosts.LocalHost, "dispatch", lambda self: dispatched.append(True))

    ctx = _ticket_ctx(product)
    assert deliver.ci_result(ctx, "battle suite", "failure", "77", "") == "rework"
    st = product.status()
    assert st["phase"] == "rework_ci" and st["ci_rounds"] == 1 and "CS1002" in st["ci_findings"] and dispatched
    assert "Die CI ist rot" in (product.run_dir / "comments.md").read_text()

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "implementer":
            seen.append((values.get("source"), str(values.get("findings", ""))))
    monkeypatch.setattr(fake, "run", run)
    assert cli.main(product.args) == 0                 # the dispatched round: rework, checks, review, judge, deliver
    st = product.status()
    assert st["phase"] == "delivered" and st["ci_findings"] is None
    assert seen and seen[0][0] == "ci" and "CS1002" in seen[0][1]
    assert [e["outcome"] for e in product.events() if e["station"] == "ci"] == ["fail"]


def test_ci_result_only_acts_on_a_delivered_ticket_at_its_head(product, monkeypatch):
    from factory import deliver, hosts
    assert cli.main(product.args) == 0
    monkeypatch.setattr(hosts.LocalHost, "dispatch", lambda self: pytest.fail("must not start a round"))
    ctx = _ticket_ctx(product)
    assert deliver.ci_result(ctx, "battle suite", "success", "1", "") == "pass"
    assert deliver.ci_result(ctx, "battle suite", "cancelled", "2", "") == "ignored"
    monkeypatch.setattr(hosts.LocalHost, "branch_head", lambda self, branch: "b" * 40)
    assert deliver.ci_result(ctx, "battle suite", "failure", "3", "a" * 40) == "stale"   # an older commit's CI
    ctx.status["phase"] = "checks"
    assert deliver.ci_result(ctx, "battle suite", "failure", "4", "") == "ignored"


def test_ci_that_stays_red_stops_and_a_restart_clears_the_count(product, monkeypatch):
    from factory import deliver, hosts
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n  pipeline:\n    max_ci_rounds: 0\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "no ci rounds")
    assert cli.main(product.args) == 0
    monkeypatch.setattr(hosts.LocalHost, "dispatch", lambda self: pytest.fail("must not start a round"))
    assert deliver.ci_result(_ticket_ctx(product), "battle suite", "failure", "5", "") == "stopped"
    st = product.status()
    assert st["phase"] == "rework_ci" and "CI" in st["stopped"]
    assert cli.main(product.args) == 0                 # the owner starts it again (label or board button)
    st = product.status()
    assert st["phase"] == "delivered" and st["ci_rounds"] == 0 and st["stopped"] is None


def test_board_restarts_a_ticket_through_the_project_workflow(monkeypatch):
    from factory import board
    calls = []

    class Done:
        def __init__(self, out=""):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    def run(argv, **kw):
        calls.append(argv)
        return Done("develop\n" if argv[:3] == ["gh", "repo", "view"] else "")
    monkeypatch.setattr(board.subprocess, "run", run)
    assert board.restart("me/game", 98)["ok"]
    assert calls[-1] == ["gh", "workflow", "run", "factory.yml", "--repo", "me/game", "--ref", "develop", "-f", "issue=98"]
    stopped = board.card("me/game", 98, {"status.json": json.dumps({"phase": "implement", "stopped": "boom"})}, None, None)
    waiting = board.card("me/game", 99, {"status.json": json.dumps({"phase": "plan", "waiting": {"reason": "plan_questions", "since": ""}})}, None, None)
    delivered = board.card("me/game", 100, {"status.json": json.dumps({"phase": "delivered"})}, None, None)
    assert (stopped["restartable"], waiting["restartable"], delivered["restartable"]) == (True, False, False)


def test_board_shows_a_restart_until_the_new_run_reports(monkeypatch):
    from factory import board
    stopped = lambda ts: {**board.card("me/game", 98, {"status.json": json.dumps({"phase": "intake", "stopped": "boom"}),
                                                     "events.jsonl": json.dumps({"ts": ts, "station": "pipeline", "kind": "pipeline"})},
                                      None, None), "source": "gh:me/game"}
    monkeypatch.setattr(board.time, "time", lambda: 1_800_000_000.0)   # 2027-01-15T08:00:00Z
    pending = {("gh:me/game", 98): 1_800_000_000.0 - 30}
    before = stopped("2027-01-15T07:00:00Z")
    board.mark_restarts([before], pending)
    assert before["running"] and before["restarting"] and not before["restartable"] and "Neustart" in before["detail"]
    after = stopped("2027-01-15T07:59:50Z")                              # the new run wrote its first event
    board.mark_restarts([after], pending)
    assert not after["running"] and after["restartable"] and "restarting" not in after


def test_failed_log_waits_for_the_run_that_reported_itself(monkeypatch):
    from factory import hosts
    host = hosts.GitHubHost.__new__(hosts.GitHubHost)
    host.repo, states, asked = "me/game", iter(["in_progress", "in_progress", "completed"]), []

    def gh(self, *args, **kw):
        asked.append(args)
        return next(states) if "--json" in args else "suite\tTests\terror CS1002"
    monkeypatch.setattr(hosts.GitHubHost, "_gh", gh)
    monkeypatch.setattr(hosts.time, "sleep", lambda s: None)
    assert host.failed_log("77").endswith("error CS1002")
    assert sum("--json" in a for a in asked) == 3 and "--log-failed" in asked[-1]


def test_every_commit_is_pushed_so_a_stopped_ticket_keeps_its_work(product, monkeypatch):
    from factory import hosts
    pushed = []
    monkeypatch.setattr(hosts.LocalHost, "push", lambda self, wt, branch: pushed.append(sh(wt, "git", "rev-parse", "HEAD")))
    assert cli.main(product.args) == 0
    branch = product.status()["branch"]
    commits = sh(product.repo, "git", "rev-list", f"main..{branch}").splitlines()
    assert commits and set(commits) <= set(pushed)       # no commit lives only on the runner


def test_a_lost_ticket_branch_starts_the_implementation_again(product, monkeypatch):
    assert cli.main(product.args) == 0
    st = product.status()
    # What a restart on a fresh runner found before every commit was pushed: no branch, no worktree,
    # and a state that says the tasks are done and the ticket stood at the checks.
    sh(product.repo, "git", "worktree", "remove", "--force", st["worktree"])
    sh(product.repo, "git", "branch", "-D", st["branch"])
    st.update(phase="checks", stopped=None, pr=None)
    (product.run_dir / "status.json").write_text(json.dumps(st))
    seen = []

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        seen.append(role)
    monkeypatch.setattr(fake, "run", run)
    assert cli.main(product.args) == 0
    st = product.status()
    assert "implementer" in seen and st["phase"] == "delivered"
    assert any(e.get("outcome") == "work_lost" for e in product.events())
    assert any("beginnt neu" in n for n in st["notes"])


def test_newer_base_commits_are_not_the_tickets_changes(product, monkeypatch):
    # A ticket branch made from a base that moved since the ticket first started (lost branch, new runner):
    # the guard must not revert the base's newer protected files, and the size limit must not count them.
    assert cli.main(product.args) == 0
    st = product.status()
    old_base = st["base_sha"]
    sh(product.repo, "git", "worktree", "remove", "--force", st["worktree"])
    sh(product.repo, "git", "branch", "-D", st["branch"])
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "# newer factory settings on the base branch\n")
    (product.repo / "BIG.md").write_text("line\n" * 500)
    sh(product.repo, "git", "add", "-A")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base moved on")
    st.update(phase="implement", done_tasks=[], stopped=None, pr=None)
    (product.run_dir / "status.json").write_text(json.dumps(st))
    assert cli.main(product.args) == 0
    st = product.status()
    branch_profile = sh(product.repo, "git", "show", f"{st['branch']}:.factory/profile.yaml")
    assert "newer factory settings" in branch_profile                      # not reverted by the guard
    assert st["base_sha"] != old_base and st["changed_lines"] < 500        # BIG.md is the base's, not the ticket's
    assert not any(e.get("outcome") == "reverted" for e in product.events())


def test_waves_shrink_and_stop_when_the_budget_cannot_pay_full_runs(product, monkeypatch):
    from factory import workers
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n  pipeline:\n    parallel_tasks: 6\n"
                       "  roles:\n    implementer:\n      budget_usd: 4.0\n  limits:\n    budget_usd_per_ticket: 9.0\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "budget")
    limits = []
    real_next = workers.next_wave
    monkeypatch.setattr(workers, "next_wave", lambda tasks, done, limit: limits.append(limit) or real_next(tasks, done, limit))
    assert cli.main(product.args) == 0
    assert limits and limits[0] == 2           # 9 $ left pays two full 4 $ runs, not six thin ones

    st = product.status()                      # the same ticket with almost nothing left: stop before trying
    st.update(phase="implement", done_tasks=[], spent_usd=7.5, budget_base=0.0, stopped=None, pr=None)
    (product.run_dir / "status.json").write_text(json.dumps(st))
    ran = []
    monkeypatch.setattr(fake, "run", lambda role, values, cwd, out: ran.append(role) or real_fake(role, values, cwd, out))
    cli.main(product.args)
    st = product.status()
    assert "implementer" not in ran and "Restbudget" in st["stopped"]


def test_protected_paths_on_the_branch_are_restored_before_the_checks(product):
    # A branch that already carries a change to a protected path (committed before the guard knew the right
    # base) gets it undone before checks and review, so it never reaches the pull request.
    assert cli.main(product.args) == 0
    st = product.status()
    wt = Path(st["worktree"])
    (wt / ".factory" / "profile.yaml").write_text("schema: profile/1\nname: reverted\nchecks: []\n")
    sh(wt, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "old profile slipped in")
    st.update(phase="checks", pr=None)
    (product.run_dir / "status.json").write_text(json.dumps(st))
    assert cli.main(product.args) == 0
    assert "name: tiny" in sh(product.repo, "git", "show", f"{product.status()['branch']}:.factory/profile.yaml")


def test_held_pull_request_runs_are_released_instead_of_started_again(product, monkeypatch):
    from factory import hosts
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n  delivery:\n    trigger_workflows: [ci.yml]\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "ci")
    monkeypatch.setattr(hosts.LocalHost, "branch_head", lambda self, branch: "abc123")
    monkeypatch.setattr(hosts.LocalHost, "approve_waiting_runs", lambda self, branch, sha, wait_s=45: ["battle suite", "lint"])
    assert cli.main(product.args) == 0
    assert not (product.run_dir / "workflows.jsonl").exists()          # no second copy of the same CI
    assert [e["detail"] for e in product.events() if e.get("outcome") == "checks_released"] == ["battle suite, lint"]


def test_github_host_approves_only_held_runs_of_the_head(monkeypatch):
    from factory import hosts
    host = hosts.GitHubHost.__new__(hosts.GitHubHost)
    host.repo, approved = "me/game", []
    runs = [{"databaseId": 1, "conclusion": "action_required", "event": "pull_request", "headSha": "new", "workflowName": "battle suite"},
            {"databaseId": 2, "conclusion": "action_required", "event": "pull_request", "headSha": "old", "workflowName": "lint"},
            {"databaseId": 3, "conclusion": "success", "event": "workflow_dispatch", "headSha": "new", "workflowName": "lint"}]
    monkeypatch.setattr(hosts.GitHubHost, "_gh", lambda self, *a, **kw: json.dumps(runs))
    monkeypatch.setattr(hosts.GitHubHost, "_api", lambda self, method, path, body=None, check=True: approved.append(path) or {})
    assert host.approve_waiting_runs("factory/1-x", "new", wait_s=0) == ["battle suite"]
    assert approved == ["actions/runs/1/approve"]


def test_each_run_takes_in_the_newer_base_first(product):
    assert cli.main(product.args) == 0
    st = product.status()
    (product.repo / "tools.md").write_text("a new tool on the base\n")
    sh(product.repo, "git", "add", "-A")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "new tool")
    st.update(phase="checks", pr=None)
    (product.run_dir / "status.json").write_text(json.dumps(st))
    assert cli.main(product.args) == 0
    files = sh(product.repo, "git", "ls-tree", "-r", "--name-only", product.status()["branch"]).splitlines()
    assert "tools.md" in files
    assert any(e.get("outcome") == "base_merged" for e in product.events())
    assert product.status()["changed_lines"] < 50     # the merged base is not counted as the ticket's change


def test_the_sweeping_run_is_not_taken_for_a_tickets_run(monkeypatch):
    from factory import hosts
    host = hosts.GitHubHost.__new__(hosts.GitHubHost)
    host.repo = "me/game"
    runs = [{"databaseId": 900, "status": "in_progress", "createdAt": "2026-11-18T02:33:00Z"},
            {"databaseId": 901, "status": "queued", "createdAt": "2026-11-18T02:34:00Z"},
            {"databaseId": 800, "status": "completed", "createdAt": "2026-11-17T22:00:00Z"}]
    monkeypatch.setattr(hosts.GitHubHost, "_gh", lambda self, *a, **kw: json.dumps(runs))
    monkeypatch.setattr(hosts.GitHubHost, "caller_workflow", lambda self: "factory.yml")
    monkeypatch.setenv("GITHUB_RUN_ID", "900")
    assert [r["id"] for r in host.active_runs()] == ["901"]


def test_a_full_disk_stops_the_ticket_before_it_fills_up(product, monkeypatch):
    from collections import namedtuple
    from factory import checks, orchestrator
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n  limits:\n    min_free_disk_gb: 6\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "disk")
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(checks.shutil, "disk_usage", lambda p: usage(100e9, 98e9, 2e9))
    monkeypatch.setattr(checks.time, "sleep", lambda s: None)
    real_wait = checks.wait_for_disk
    monkeypatch.setattr(checks, "wait_for_disk", lambda ctx, path: real_wait(ctx, path, wait_minutes=0))
    ran = []
    monkeypatch.setattr(fake, "run", lambda role, values, cwd, out: ran.append(role) or real_fake(role, values, cwd, out))
    cli.main(product.args)
    st = product.status()
    assert "zu wenig freier Speicher" in st["stopped"] and "implementer" not in ran
    assert orchestrator._infrastructure(st)          # the orchestrator tries it again later


def test_enough_disk_lets_the_ticket_through(product, monkeypatch):
    from collections import namedtuple
    from factory import checks
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n  limits:\n    min_free_disk_gb: 6\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "disk")
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(checks.shutil, "disk_usage", lambda p: usage(100e9, 50e9, 50e9))
    assert cli.main(product.args) == 0 and product.status()["phase"] == "delivered"


def test_agents_without_a_sandbox_are_told_which_programs_they_may_start(product, monkeypatch):
    from factory import stations
    monkeypatch.delenv("FACTORY_SANDBOXED", raising=False)
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text().replace("agent_commands: [python3]", "agent_commands: [python3, tools/ci/unity.sh]"))
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "unity")
    prompts = []
    real = stations.commands_note
    monkeypatch.setattr(stations, "commands_note", lambda ctx, rc: prompts.append((rc.get("tools"), real(ctx, rc))) or prompts[-1][1])
    assert cli.main(product.args) == 0
    writer = [p for tools, p in prompts if tools == "write"]
    reader = [p for tools, p in prompts if tools == "read"]
    assert writer and all("`tools/ci/unity.sh`" in p and "not allowed, not broken" in p for p in writer)
    assert reader and all("`git diff`" in p and "unity.sh" not in p for p in reader)
    monkeypatch.setenv("FACTORY_SANDBOXED", "1")
    assert real(None, {"tools": "write"}) == ""


def test_a_long_run_hands_the_ticket_to_a_new_run_between_tasks(product, monkeypatch):
    from factory import hosts, pipeline
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n  pipeline:\n    max_run_minutes: 1\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "short runs")
    dispatched = []
    monkeypatch.setattr(hosts.LocalHost, "dispatch", lambda self: dispatched.append(True))
    import time as real_time
    clock = {"now": real_time.time()}
    monkeypatch.setattr(pipeline.time, "time", lambda: clock["now"])

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "planner":
            clock["now"] += 120            # planning took two minutes: the run is over its limit
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    st = product.status()
    assert dispatched and st["phase"] == "implement" and st["stopped"] is None and not st["done_tasks"]
    assert [e["outcome"] for e in product.events()][-1] == "handover"


def test_a_station_the_budget_cannot_pay_stops_the_ticket_before_it_runs(product, monkeypatch):
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n  roles:\n    reviewer:\n      budget_usd: 2.5\n"
                       "  limits:\n    budget_usd_per_ticket: 5.0\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "budget")
    assert cli.main(product.args) == 0
    st = product.status()
    st.update(phase="checks", spent_usd=4.8, budget_base=0.0, pr=None, stopped=None)   # 0.20 $ left, review needs 2.50 $
    (product.run_dir / "status.json").write_text(json.dumps(st))
    ran = []
    monkeypatch.setattr(fake, "run", lambda role, values, cwd, out: ran.append(role) or real_fake(role, values, cwd, out))
    cli.main(product.args)
    st = product.status()
    assert "reviewer" not in ran and st["stopped"] == "Budget des Tickets aufgebraucht"


def _review_rounds(product, monkeypatch, counts):
    gate(product, "review")
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "    max_rework_rounds: 2\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "two rounds")
    seen = {"n": 0}

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "reviewer":
            n = counts[min(seen["n"], len(counts) - 1)]
            seen["n"] += 1
            if n:
                items = "".join(f"  - file: a.py\n    text: problem {i}\n" for i in range(n))
                out.write_text(f"---\nschema: review/1\nverdict: changes\nblocking:\n{items}minor: []\n---\n")
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    return product.status()


def test_a_review_that_converges_gets_rounds_past_the_limit(product, monkeypatch):
    st = _review_rounds(product, monkeypatch, [4, 3, 2, 1, 0])     # fewer open points every round
    assert st["phase"] == "delivered" and st["rework_rounds"] == 4 and st["stopped"] is None


def _escalated(product):
    return [e for e in product.events() if e["station"] == "review" and e.get("outcome") == "escalated"]


def test_a_review_that_does_not_converge_is_delivered_with_its_open_points(product, monkeypatch):
    st = _review_rounds(product, monkeypatch, [2, 2, 2, 2, 2])
    assert st["phase"] == "delivered" and st["stopped"] is None and st["rework_rounds"] == 2
    assert _escalated(product) and any("ohne Fortschritt" in n for n in st["notes"])
    body = (product.run_dir / "pr.md").read_text()
    assert "Review: blockierende Punkte" in body and "problem 0" in body


def test_even_a_converging_review_hands_over_at_twice_the_limit(product, monkeypatch):
    st = _review_rounds(product, monkeypatch, [9, 8, 7, 6, 5, 4, 3, 2])
    assert st["phase"] == "delivered" and st["rework_rounds"] == 4 and _escalated(product)


def test_two_review_rounds_without_progress_hand_over_before_the_limit(product, monkeypatch):
    gate(product, "review")
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "    max_rework_rounds: 5\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "five rounds")
    seen = {"n": 0}

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "reviewer":
            seen["n"] += 1
            out.write_text("---\nschema: review/1\nverdict: changes\nblocking:\n  - text: same point\nminor: []\n---\n")
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    st = product.status()
    assert st["phase"] == "delivered" and st["rework_rounds"] == 2 and seen["n"] == 3 and _escalated(product)


def test_the_reviewer_learns_which_evidence_the_implementer_can_produce(product, monkeypatch):
    from factory import stations
    gate(product, "review")
    prompts = []
    real_render = stations.render
    monkeypatch.setattr(stations, "render", lambda t, v, p=None: prompts.append((t, real_render(t, v, p))) or prompts[-1][1])
    cli.main(product.args)
    review = [p for t, p in prompts if "reviewer" in t]
    assert review and "What the implementer can produce" in review[0] and "needs a human" in review[0]


def test_the_next_review_checks_the_points_of_the_one_before(product, monkeypatch):
    from factory import stations
    gate(product, "review")
    prompts, seen = [], {"n": 0}
    real_render = stations.render
    monkeypatch.setattr(stations, "render", lambda t, v, p=None: prompts.append((t, v.get("earlier_review"))) or real_render(t, v, p))

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "reviewer":
            seen["n"] += 1
            if seen["n"] == 1:
                out.write_text("---\nschema: review/1\nverdict: changes\nblocking:\n  - file: a.py\n    line: 3\n"
                               "    text: wrong result for empty input\nminor: []\n---\n")
    monkeypatch.setattr(fake, "run", run)
    assert cli.main(product.args) == 0
    earlier = [e for t, e in prompts if t.endswith("reviewer.md")]
    assert earlier[0] == ""                                                  # first review: nothing before it
    assert "a.py:3 wrong result for empty input" in earlier[1] and "resolved or still open" in earlier[1]


def test_restoring_the_base_before_checks_does_not_hold_back_a_merge(product, monkeypatch):
    # The restore before checks undoes what the machinery left on the branch, not what an agent tried.
    assert cli.main(product.args) == 0
    st = product.status()
    wt = Path(st["worktree"])
    (wt / ".factory" / "profile.yaml").write_text("schema: profile/1\nname: stale\nchecks: []\n")
    sh(wt, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "stale base slipped in")
    st.update(phase="checks", pr=None)
    (product.run_dir / "status.json").write_text(json.dumps(st))
    assert cli.main(product.args) == 0
    guards = [e for e in product.events() if e["kind"] == "guard"]
    assert [g["outcome"] for g in guards] == ["restored"]
    assert not any(str(g.get("detail", "")).startswith("protected paths") for g in guards)


def test_without_a_scenario_repository_the_judge_writes_its_own_from_the_issue(product, monkeypatch):
    seen = {}

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "scenario_writer":
            seen["cwd"], seen["issue"] = [p.name for p in Path(cwd).iterdir() if p.name != ".git"], json.loads(Path(values["issue_path"]).read_text())
            out.write_text(json.dumps({"schema": "scenarios/1", "scenarios": [
                {"title": f"S{i}", "user": "A user.", "steps": ["Do it"], "satisfied_when": ["It works"]}
                for i in range(7)]}))
    monkeypatch.setattr(fake, "run", run)
    assert cli.main(product.args[:-2]) == 0                  # no scenarios given
    verdict = json.loads((product.run_dir / "verdict.json").read_text())
    assert verdict["scenarios_total"] == 5 and verdict["passed"] is True     # at most count
    assert "aus dem Issue erzeugt" in verdict["notes"]
    assert seen["cwd"] == []                                 # the writer sees no code
    assert set(seen["issue"]) == {"title", "body"}           # only the owner's words


def judge_fails(times):
    """A judge that finds one scenario unmet the first `times` times it runs, then is satisfied."""
    seen = {"n": 0}

    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "judge":
            seen["n"] += 1
            if seen["n"] <= times:
                manifest = json.loads(Path(values["manifest_path"]).read_text())
                out.write_text(json.dumps({"schema": "judge-result/1", "results": [
                    {"id": manifest[0]["id"], "satisfied": False, "symptom": "the list stays empty after adding"},
                    {"id": manifest[1]["id"], "satisfied": True}]}))
    return run, seen


def advisory_rounds(product, n):
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + f"overrides:\n  pipeline:\n    advisory_judge_rounds: {n}\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "advisory rounds")


def test_an_advisory_judge_gets_its_automatic_rounds_then_the_pr_comes_anyway(product, monkeypatch):
    advisory_rounds(product, 1)
    run, seen = judge_fails(99)
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    st = product.status()
    assert (st["phase"], st["round"], st["judge_auto_rounds"]) == ("rework_judge", 2, 1)
    assert "bessert automatisch nach (Runde 1 von 1)" in (product.run_dir / "comments.md").read_text()
    cli.main(product.args)                                  # round two fails again: delivered as it stands
    st = product.status()
    assert st["phase"] == "delivered" and seen["n"] == 2 and not st.get("stopped")
    assert json.loads((product.run_dir / "verdict.json").read_text())["passed"] is False


def test_an_advisory_judge_without_automatic_rounds_delivers_at_once(product, monkeypatch):
    run, seen = judge_fails(99)
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    assert product.status()["phase"] == "delivered" and seen["n"] == 1


def test_the_owner_sends_a_delivered_pr_back_with_the_judges_findings(product, monkeypatch):
    from factory import hosts
    run, seen = judge_fails(1)
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    assert product.status()["phase"] == "delivered"
    cli.main(product.args)                                  # without the label nothing happens
    assert seen["n"] == 1

    real_issue = hosts.LocalHost.issue
    asked = {"label": True}
    monkeypatch.setattr(hosts.LocalHost, "issue", lambda self: {**real_issue(self),
                        "labels": ["factory:rework"] if asked["label"] else []})
    monkeypatch.setattr(hosts.LocalHost, "labels", lambda self, add=(), remove=(): asked.update(
        label=asked["label"] and "factory:rework" not in remove))
    hosts.LocalHost.answer(type("H", (), {"_human": lambda s: product.run_dir / "human-comments.jsonl"})(),
                           "Please also show the count.")
    cli.main(product.args)
    st = product.status()
    assert st["phase"] == "delivered" and st["round"] == 2 and seen["n"] == 2 and not asked["label"]
    comments = (product.run_dir / "comments.md").read_text()
    assert "**Nachbessern:**" in comments and "und deinen Hinweisen" in comments
    assert [e["station"] for e in product.events() if e["kind"] == "agent"][-2:] == ["rework", "reviewer"]


def test_a_skill_with_a_condition_is_offered_with_it_and_not_forced():
    # Every skill read costs context: the implementer gets each skill's condition and loads only what its work reaches.
    cfg = yaml.safe_load((ROOT / "factory.yaml").read_text())
    rc = cfg["roles"]["implementer"]
    assert set(rc["skill_when"]) <= set(rc["skills"])          # a condition for a skill the role does not offer is a typo

    from factory.harness import get
    claude = get("claude")
    assert "only when this run reaches" in claude.skill_instruction(True)
    assert "only when" not in claude.skill_instruction(False)

    from factory import stations
    note = stations.skills_note(rc, ["superpowers:test-driven-development", "simplify"], claude)
    assert "- simplify: once, when all your tasks are done" in note
    assert "- superpowers:test-driven-development: when a task adds or changes behaviour" in note
    assert "only when this run reaches" in note
    # a role without conditions keeps the old, firm wording
    assert "only when" not in stations.skills_note({"skills": ["code-review"]}, ["code-review"], claude)
    assert stations.skills_note(rc, [], claude) == ""
