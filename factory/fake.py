"""Dry run: canned station outputs instead of model calls.

Exercises the whole pipeline, including GitHub plumbing, without spending
tokens. Switched on with FACTORY_FAKE_AGENT=1. Says nothing about whether the
prompts work.
"""
from __future__ import annotations

import json
from pathlib import Path


def run(role: str, values: dict, cwd: Path, out: Path | None) -> None:
    if role == "intake":
        out.write_text(json.dumps({
            "schema": "ticket/1", "id": int(values["issue_number"]), "title": str(values["issue_title"]),
            "status": "ready", "mandate": "Dry run: no model was asked.",
            "acceptance": ["The pipeline runs from intake to delivery."], "size": "S", "sensitive": False,
        }, indent=2))
    elif role == "planner":
        out.write_text(
            f"---\nschema: plan/1\nticket: {values['ticket']}\nsummary: Dry run plan.\n"
            "tasks:\n  - id: T1\n    title: Leave a dry run marker\n    goal: Prove that a change travels through the pipeline.\n"
            "    acceptance: [\"FACTORY_DRYRUN.md exists\"]\n    files: [FACTORY_DRYRUN.md]\n---\n\nDry run.\n")
    elif role == "backlog":
        out.write_text(json.dumps({"schema": "backlog/1", "proposals": []}))
    elif role == "plan_check":
        out.write_text(json.dumps({"schema": "plan-review/1", "verdict": "pass", "findings": []}))
    elif role == "implementer":
        marker = Path(cwd) / "FACTORY_DRYRUN.md"
        previous = marker.read_text() if marker.exists() else "# Dry run\n"
        marker.write_text(previous + f"\n- pass {previous.count(chr(10) + '- pass') + 1}\n")
    elif role == "checks":
        out.write_text("Dry run: no triage.\n")
    elif role == "reviewer":
        out.write_text("---\nschema: review/1\nverdict: approve\nblocking: []\nminor: []\n---\n\nDry run.\n")
    elif role == "scenario_writer":
        out.write_text(json.dumps({"schema": "scenarios/1", "scenarios": [
            {"title": "Dry run", "user": "The owner wants to see the pipeline work.",
             "steps": ["Open the change"], "satisfied_when": ["It is there"]}]}))
    elif role == "judge":
        manifest = json.loads(Path(values["manifest_path"]).read_text())
        out.write_text(json.dumps({"schema": "judge-result/1",
                                   "results": [{"id": m["id"], "satisfied": True, "symptom": ""} for m in manifest]}))
    else:
        raise ValueError(f"no dry run output for role {role}")
