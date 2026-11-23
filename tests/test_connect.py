"""Connecting a repository from the settings page: what is built from the form, and what is asked of GitHub."""
import json

import pytest
import yaml

from factory import config, connect, contracts
from factory.config import ROOT
from factory.settings import SettingsError


def test_checks_are_read_one_per_line_with_or_without_a_name():
    checks = connect.parse_checks("tests: python3 -m pytest -q\n\nnpm run lint\nbuild: make all: fast\n")
    assert [(c["name"], c["run"]) for c in checks] == [
        ("tests", "python3 -m pytest -q"), ("check-2", "npm run lint"), ("build", "make all: fast")]


def test_setup_and_checks_are_guessed_from_the_files_in_the_root():
    assert connect.suggest({"package.json", "src"})["checks"] == "tests: npm test"
    assert connect.suggest({"requirements.txt", "tests"})["setup"].endswith("requirements.txt")
    assert connect.suggest({"Assets", "ProjectSettings", "Packages"})["capability"] == "self-hosted"
    assert connect.suggest({"README.md"})["checks"] == ""


def test_profile_from_the_form_fits_the_contract():
    cfg = config.load_config()
    text = connect.build_profile("me/shop", {"description": "Ein Shop.", "setup": "npm ci", "checks": "tests: npm test",
                                             "capability": "linux", "scenarios_repo": "me/shop-scenarios",
                                             "trigger_workflows": ["ci.yml"]}, cfg)
    profile = contracts.validate(yaml.safe_load(text), "profile")
    assert profile["name"] == "shop" and profile["setup"] == "npm ci"
    assert profile["checks"] == [{"name": "tests", "run": "npm test", "timeout_minutes": 30}]
    assert profile["judge"]["scenarios_repo"] == "me/shop-scenarios"
    assert profile["overrides"] == {"delivery": {"trigger_workflows": ["ci.yml"]}}
    assert "runtime" not in profile and "agent_commands" not in profile   # the default runner needs neither


def test_profile_for_an_own_runner_names_what_agents_may_start():
    cfg = config.load_config()
    profile = yaml.safe_load(connect.build_profile("me/game", {"checks": "suite: tools/ci/test.sh --fast\npython3 lint.py",
                                                               "capability": "self-hosted"}, cfg))
    assert profile["runtime"] == {"work": "self-hosted", "judge": "linux"}
    assert profile["agent_commands"] == ["python3", "tools/ci/test.sh"]


def test_wrong_input_is_refused_before_anything_is_changed():
    cfg = config.load_config()
    with pytest.raises(SettingsError, match="Mindestens ein Check"):
        connect.build_profile("me/x", {"checks": "  \n"}, cfg)
    with pytest.raises(SettingsError, match="kennt die Factory nicht"):
        connect.build_profile("me/x", {"checks": "make test", "capability": "mainframe"}, cfg)
    with pytest.raises(SettingsError, match="passen nicht"):
        connect.build_profile("me/x", {"checks": "make test", "trigger_workflows": ["not a workflow"]}, cfg)
    with pytest.raises(SettingsError, match="owner/name"):
        connect.inspect("just-a-name")


def test_workflow_names_the_base_branch_only_when_it_is_not_the_default():
    assert "base: develop" not in connect.build_workflow("main", "main").replace("# base: develop", "").replace("#   base: develop", "")
    other = connect.build_workflow("develop", "master")
    assert other.count("      base: develop\n") == 4 and "# base: develop" not in other and "#   base: develop" not in other
    assert yaml.safe_load(other)["jobs"]["orchestrate"]["with"] == {"base": "develop"}
    assert "workflow_run" not in yaml.safe_load(other)[True]   # no CI named: the trigger stays commented out
    ci = yaml.safe_load(connect.build_workflow("develop", "master", ["lint", "battle suite"]))
    assert ci[True]["workflow_run"] == {"workflows": ["lint", "battle suite"], "types": ["completed"], "branches": ["factory/**"]}
    assert ci["jobs"]["ci"]["with"]["base"] == "develop"


class FakeGitHub:
    """Answers the calls connect makes, and remembers what was asked."""

    def __init__(self, default="main", profile=None, workflow=None, labels=(), may_open=False, secrets=()):
        self.default, self.profile, self.workflow = default, profile, workflow
        self.labels, self.may_open, self.secrets = list(labels), may_open, list(secrets)
        self.calls, self.prs = [], []

    def gh(self, *args, stdin=None):
        self.calls.append(args)
        if args[:2] == ("api", "graphql"):
            return json.dumps({"data": {"repository": {
                "nameWithOwner": "me/shop", "isPrivate": True, "viewerPermission": "ADMIN", "description": "Ein Shop.",
                "defaultBranchRef": {"name": self.default},
                "refs": {"nodes": [{"name": self.default}, {"name": "develop"}]},
                "labels": {"nodes": [{"name": n} for n in self.labels]},
                "profile": self.profile and {"text": self.profile}, "workflow": self.workflow and {"text": self.workflow},
                "root": {"entries": [{"name": "package.json"}]},
                "pullRequests": {"nodes": [{"url": u, "title": "chore(factory): attach", "headRefName": h, "baseRefName": b}
                                           for u, h, b in self.prs]}}}})
        if args[:2] == ("secret", "list"):
            return json.dumps([{"name": n} for n in self.secrets])
        if args[0] == "label":
            self.labels.append(args[2])
            return ""
        method, path = args[2], args[3]
        body = json.loads(stdin) if stdin else None
        if path.endswith("/actions/permissions/workflow"):
            if method == "PUT":
                self.may_open = body["can_approve_pull_request_reviews"]
                return ""
            return json.dumps({"can_approve_pull_request_reviews": self.may_open})
        if "/git/ref/heads/" in path:
            return json.dumps({"object": {"sha": "tip"}})
        if "/git/commits/" in path:
            return json.dumps({"tree": {"sha": "tree0"}})
        if path.endswith("/git/trees"):
            self.tree = {e["path"]: e["content"] for e in body["tree"]}
            return json.dumps({"sha": "tree1"})
        if path.endswith("/git/commits"):
            return json.dumps({"sha": "commit1"})
        if path.endswith("/git/refs"):
            return json.dumps({"ref": body["ref"]})
        if path.endswith("/pulls"):
            url = f"https://github.com/me/shop/pull/{len(self.prs) + 1}"
            self.prs.append((url, body["head"], body["base"]))
            return json.dumps({"html_url": url})
        raise AssertionError(f"unexpected call: {args}")


@pytest.fixture
def github(monkeypatch, tmp_path):
    fake = FakeGitHub()
    monkeypatch.setattr(connect, "_gh", fake.gh)
    monkeypatch.setenv("FACTORY_SETTINGS", str(tmp_path / "settings.yaml"))
    monkeypatch.setattr(connect.settings, "publish", lambda: {"published": False, "message": "nur lokal (Test)"})
    return fake


def test_a_fresh_repository_gets_every_step_and_one_pull_request(github):
    before = connect.inspect("me/shop", None, [])
    assert [s["ok"] for s in before["steps"]] == [False, False, False, False, False, False] and not before["connected"]
    assert before["suggestion"]["checks"] == "tests: npm test"

    board = []
    result = connect.connect("me/shop", {"checks": "tests: npm test", "setup": "npm ci", "capability": "linux"}, board)
    assert [d["ok"] for d in result["done"]] == [True, True, True, True]
    assert board == ["me/shop"] and "me/shop" in config.load_config()["board"]["repos"]
    assert len(github.labels) == 8 and github.may_open is True
    assert sorted(github.tree) == [".factory/profile.yaml", ".github/ISSUE_TEMPLATE/factory-ticket.md", ".github/workflows/factory.yml"]
    assert yaml.safe_load(github.tree[".factory/profile.yaml"])["checks"][0]["run"] == "npm test"
    assert github.prs == [("https://github.com/me/shop/pull/1", connect.BRANCH, "main")]

    after = {s["id"]: s for s in result["after"]["steps"]}
    assert after["labels"]["ok"] and after["permission"]["ok"] and after["board"]["ok"]
    assert after["profile"]["pull_request"].endswith("/pull/1")   # waits for the owner's merge
    assert after["secret"]["ok"] is False and "gh secret set" in after["secret"]["command"]


def test_another_base_branch_needs_the_workflow_on_the_default_branch_too(github):
    github.default = "master"
    result = connect.connect("me/shop", {"checks": "npm test", "base": "develop"}, [])
    assert all(d["ok"] for d in result["done"])
    assert [(head, base) for _, head, base in github.prs] == [(connect.BRANCH, "develop"), (connect.BRANCH_DEFAULT, "master")]
    assert "      base: develop\n" in github.tree[".github/workflows/factory.yml"]


def test_a_connected_repository_is_left_alone(github):
    github.profile, github.workflow, github.may_open = "schema: profile/1", "name: factory", True
    github.labels, github.secrets = [name for name, _, _ in connect.LABELS], ["CLAUDE_CODE_OAUTH_TOKEN"]
    info = connect.inspect("me/shop", None, ["me/shop"])
    assert info["connected"] and all(s["ok"] for s in info["steps"])
    github.calls.clear()
    result = connect.connect("me/shop", {}, ["me/shop"])
    assert result["done"] == [] and not [c for c in github.calls if c[0] == "label" or "POST" in c or "PUT" in c]


def test_pull_requests_made_by_hand_count_and_show_the_base_branch(github):
    github.default = "master"
    github.prs = [("https://github.com/me/shop/pull/7", "agent/attach-by-hand", "develop"),
                  ("https://github.com/me/shop/pull/8", "agent/workflow-on-master", "master")]
    info = connect.inspect("me/shop", None, [])
    steps = {s["id"]: s for s in info["steps"]}
    assert info["base"] == "develop"
    assert steps["profile"]["pull_request"].endswith("/pull/7") and steps["workflow"]["pull_request"].endswith("/pull/8")
    result = connect.connect("me/shop", {}, [])   # only labels, the Actions right and the board are left to do
    assert [d["title"] for d in result["done"]] == ["Labels angelegt", "Actions dürfen Pull Requests öffnen", "Ins Board aufgenommen"]
    assert len(github.prs) == 2


def test_a_project_can_be_taken_off_the_board(github):
    board = []
    connect.connect("me/shop", {"checks": "npm test"}, board)
    assert "me/shop" in config.load_config()["board"]["repos"]
    connect.remove_from_board("me/shop", board)
    assert board == [] and "me/shop" not in config.load_config()["board"]["repos"]


def test_ci_results_reach_the_factory_by_dispatch():
    # A CI run the job token started triggers no workflow_run; report-ci.yml dispatches the result instead.
    wf = yaml.safe_load(connect.build_workflow("develop", "master", ["battle suite"]))
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert inputs["issue"]["required"] is False
    assert {"ci_branch", "ci_workflow", "ci_conclusion", "ci_run_id", "ci_sha"} <= set(inputs)
    assert "!inputs.ci_branch" in wf["jobs"]["ticket"]["if"]                       # a CI result never starts a ticket
    assert "startsWith(inputs.ci_branch, 'factory/')" in wf["jobs"]["ci"]["if"]
    assert "inputs.ci_branch" in wf["concurrency"]["group"]
    relay = yaml.safe_load((ROOT / ".github/workflows/report-ci.yml").read_text())
    assert "-f ci_branch=" in relay["jobs"]["report"]["steps"][0]["run"]


def test_caller_grants_every_permission_its_called_workflows_ask_for():
    # A called workflow that asks for more than its caller grants makes GitHub reject the whole caller.
    import re
    caller = yaml.safe_load(connect.build_workflow("develop", "master", ["battle suite"]))
    granted = caller["permissions"]
    rank = {"none": 0, "read": 1, "write": 2}
    for job in caller["jobs"].values():
        m = re.match(r"HanSur94/software-factory/(\.github/workflows/[\w.-]+)@", job.get("uses", ""))
        if not m:
            continue
        called = yaml.safe_load((ROOT / m.group(1)).read_text())
        for cj in called["jobs"].values():
            for scope, level in (cj.get("permissions") or {}).items():
                assert rank[granted.get(scope, "none")] >= rank[level], f"{m.group(1)} asks for {scope}: {level}"
