"""Contract tests: example inputs with the expected verdict. A replacement for a
subsystem has to produce files that pass the same checks."""
import json

import pytest
import yaml

from factory import contracts
from factory.config import ROOT, load_config
from factory.contracts import ContractError

TICKET = {"schema": "ticket/1", "id": 3, "title": "Add remove", "status": "ready", "mandate": "Do it.",
          "acceptance": ["It works."], "size": "S", "sensitive": False}


def test_ticket_ready_fits():
    contracts.validate(TICKET, "ticket")


@pytest.mark.parametrize("change", [
    {"status": "needs_info"},                      # questions missing
    {"status": "rejected"},                        # reason missing
    {"acceptance": []},                            # ready without criteria
    {"size": "XL"},
    {"schema": "ticket/2"},
    {"extra": 1},
])
def test_ticket_violations(change):
    with pytest.raises(ContractError):
        contracts.validate({**TICKET, **change}, "ticket")


def test_ticket_needs_info_with_questions_fits():
    contracts.validate({**TICKET, "status": "needs_info", "mandate": "", "acceptance": [], "questions": ["Which?"]}, "ticket")


def test_plan_front_matter(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("---\nschema: plan/1\nticket: 3\nsummary: s\ntasks:\n  - id: T1\n    title: t\n    goal: g\n"
                    "    acceptance: [a]\n---\n\n# Plan\ntext\n")
    meta, body = contracts.read_frontmatter(plan, "plan")
    assert meta["tasks"][0]["id"] == "T1" and "# Plan" in body


@pytest.mark.parametrize("text", [
    "# Plan\nno front matter\n",
    "---\nschema: plan/1\nticket: 3\nsummary: s\ntasks: []\n---\n",                      # no tasks
    "---\nschema: plan/1\nticket: 3\nsummary: s\ntasks:\n  - id: one\n    title: t\n    goal: g\n    acceptance: [a]\n---\n",
    "---\nschema: plan/1\nticket: 3\nsummary: [unclosed\n---\n",
])
def test_plan_violations(tmp_path, text):
    plan = tmp_path / "plan.md"
    plan.write_text(text)
    with pytest.raises(ContractError):
        contracts.read_frontmatter(plan, "plan")


def test_missing_file_is_a_violation(tmp_path):
    with pytest.raises(ContractError):
        contracts.read_json(tmp_path / "ticket.json", "ticket")


def test_verdict_allows_no_judgement():
    contracts.validate({"schema": "verdict/1", "ticket": 1, "commit": "abc", "scenarios_total": 0,
                        "scenarios_passed": 0, "satisfaction": None, "threshold": 0.9, "passed": None,
                        "failures": []}, "verdict")


def test_every_schema_is_a_valid_schema():
    from jsonschema import Draft202012Validator
    for path in (ROOT / "contracts").glob("*.schema.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text()))


def test_config_and_templates_are_consistent():
    cfg = load_config()
    for role, rc in cfg["roles"].items():
        for key in ("prompt", "rework_prompt"):
            if key in rc:
                assert (ROOT / rc[key]).exists(), f"{role}.{key}"
    for station in cfg["pipeline"]["modes"]:
        assert cfg["pipeline"]["modes"][station] in ("advisory", "gate", "off")
    for capability in cfg["runtime"]["jobs"].values():
        assert capability in cfg["runtime"]["capabilities"]
    profile = yaml.safe_load((ROOT / "templates/product/.factory/profile.yaml").read_text())
    contracts.validate(profile, "profile")
