"""Stopping a whole project from the board: the workflow goes off first, then every unfinished run is cancelled."""
import json
import os

from factory import board, keepalive

# A stand-in for gh: one factory run in progress, one CI run on a factory branch, one unrelated run.
FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.path.join(os.path.dirname(__file__), "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
if args[:1] == ["api"] and "actions/workflows/factory.yml/runs?status=in_progress" in args[-1]:
    print(json.dumps({"workflow_runs": [{"id": 41, "head_branch": "main"}]}))
elif args[:1] == ["api"] and args[-1].startswith("repos/o/r/actions/runs?status=in_progress"):
    print(json.dumps({"workflow_runs": [{"id": 41, "head_branch": "main"}, {"id": 52, "head_branch": "factory/7-x"},
                                        {"id": 60, "head_branch": "feature/mine"}]}))
elif args[:1] == ["api"] and "runs?" in args[-1]:
    print(json.dumps({"workflow_runs": []}))
elif args[:1] == ["api"] and args[-1].endswith("workflows/factory.yml"):
    print(json.dumps({"state": "disabled_manually"}))
elif args[:2] == ["workflow", "run"]:
    pass
"""


def fake_gh(tmp_path, monkeypatch):
    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    board._paused.clear()
    keepalive.STATE.clear()
    return lambda: [" ".join(json.loads(line)) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


def test_pause_switches_the_workflow_off_first_and_cancels_the_factory_runs(tmp_path, monkeypatch):
    calls = fake_gh(tmp_path, monkeypatch)
    result = board.pause_project("o/r", force_after=0)
    text = calls()
    off = text.index("workflow disable factory.yml --repo o/r")
    first_cancel = next(i for i, t in enumerate(text) if t.startswith("run cancel"))
    assert off < first_cancel                                        # nothing new starts while the runs are cancelled
    assert sorted(t for t in text if t.startswith("run cancel")) == ["run cancel 41 --repo o/r", "run cancel 52 --repo o/r"]
    assert result["ok"] and "2 Läufe gestoppt" in result["message"]
    assert board.project_paused("o/r") and keepalive.STATE["o/r"]["paused"]


def test_a_paused_project_gets_no_orchestrator_from_the_keepalive():
    started = keepalive.tick(["o/r"], {"o/r": {"kicked": 0.0, "paused": True}}, now=10_000.0, minutes=40,
                             last=lambda repo: 0.0, start=lambda repo: "ok")
    assert started == []


def test_resume_switches_on_and_starts_the_orchestrator(tmp_path, monkeypatch):
    calls = fake_gh(tmp_path, monkeypatch)
    keepalive.STATE["o/r"] = {"kicked": 0.0, "paused": True}
    result = board.resume_project("o/r")
    text = calls()
    on = text.index("workflow enable factory.yml --repo o/r")
    kick = next(i for i, t in enumerate(text) if t.startswith("workflow run factory.yml"))
    assert on < kick and result["ok"] and "läuft wieder" in result["message"]
    assert keepalive.STATE["o/r"]["paused"] is False and not board._paused["o/r"][1]


def test_a_workflow_switched_off_on_github_counts_as_paused():
    state = {"o/r": {"kicked": 0.0}}
    keepalive.tick(["o/r"], state, now=10_000.0, minutes=40, last=lambda repo: 0.0,
                   start=lambda repo: "HTTP 422: Cannot trigger a 'workflow_dispatch' on a disabled workflow")
    assert state["o/r"]["paused"] is True
