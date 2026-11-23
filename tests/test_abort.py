"""Aborting a ticket by hand: it stops in any phase, stays stopped, and the restart button carries on."""
import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

from conftest import sh  # noqa: F401 - the product fixture
from factory import board, checks, cli, events, hosts, orchestrator

ABORTED = "factory:aborted"


def ticket_ctx(product, n=1):
    return cli.make_ctx(SimpleNamespace(repo=str(product.repo), issue=n, issue_file=str(product.issue), budget=None), local=True)


def test_an_aborted_ticket_does_not_start(product, monkeypatch):
    real = hosts.LocalHost.issue
    monkeypatch.setattr(hosts.LocalHost, "issue", lambda self: {**real(self), "labels": [ABORTED]})
    assert cli.main(product.args) == 0
    st = product.status()
    assert st["stopped"] == "Von Hand abgebrochen" and st["phase"] == "intake"
    assert [e["outcome"] for e in events.read(product.run_dir) if e["station"] == "human"] == ["aborted"]


def test_a_running_ticket_stops_at_its_next_phase(product, monkeypatch):
    asked = []

    def labels(self):
        asked.append(1)
        return [ABORTED] if len(asked) >= 3 else []
    monkeypatch.setattr(hosts.LocalHost, "current_labels", labels, raising=False)
    assert cli.main(product.args) == 0
    st = product.status()
    assert st["stopped"] == "Von Hand abgebrochen" and st["phase"] == "plan_check" and st["current"] is None
    assert not (product.run_dir / "pr.md").exists()


def test_the_orchestrator_leaves_an_aborted_ticket_alone(product, monkeypatch):
    assert cli.main(product.args) == 0
    status = product.status()
    status.update(phase="implement", stopped="unerwarteter Fehler im Steuerskript:\nGitError")   # a machinery stop
    (product.run_dir / "status.json").write_text(json.dumps(status))
    monkeypatch.setattr(orchestrator, "_age_minutes", lambda ts: 999.0)
    monkeypatch.setattr(hosts.LocalHost, "open_issues", lambda self, label: [{"number": 1}] if label == ABORTED else [])
    done = orchestrator.sweep(ticket_ctx(product), lambda n: ticket_ctx(product, n))
    assert done["restarted"] == [] and not (product.run_dir / "dispatched.jsonl").exists()


def test_only_the_tickets_own_runs_are_cancelled():
    active = [{"databaseId": 11, "status": "in_progress", "event": "workflow_dispatch", "displayTitle": "factory"},
              {"databaseId": 12, "status": "queued", "event": "issue_comment", "displayTitle": "Moral bricht"},
              {"databaseId": 13, "status": "in_progress", "event": "issues", "displayTitle": "Anderes Ticket"},
              {"databaseId": 14, "status": "in_progress", "event": "workflow_dispatch", "displayTitle": "factory"}]
    urls = ["https://github.com/o/r/actions/runs/11", "https://github.com/o/r/actions/runs/9"]
    assert board.runs_of(5, "Moral bricht", urls, active) == ["11", "12"]


def test_the_card_says_aborted_and_no_station_runs():
    status = {"phase": "implement", "current": "implementer", "run_urls": []}
    files = {"status.json": json.dumps(status), "events.jsonl": ""}
    c = board.card("o/r", 5, files, {"title": "t", "url": "u", "labels": [ABORTED, "factory:running"]}, None)
    assert c["column"] == "stopped" and c["step"] == "abgebrochen" and c["aborted"] and not c["running"]
    assert "Neu starten" in c["detail"]
    assert not [s for s in c["workflow"]["stations"] if s["state"] == "active"]
    assert board.card("o/r", 5, files, {"title": "t", "url": "u", "labels": ["factory:running"]}, None)["aborted"] is False


FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
log = os.path.join(os.path.dirname(__file__), "calls.jsonl")
open(log, "a").write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:2] == ["run", "list"]:
    print(json.dumps([{"databaseId": 41, "status": "in_progress", "event": "workflow_dispatch", "displayTitle": "factory"},
                      {"databaseId": 42, "status": "in_progress", "event": "issues", "displayTitle": "other"}]))
elif args[:1] == ["api"] and args[-1].endswith("status.json?ref=factory-state"):
    print(json.dumps({"phase": "implement", "run_urls": ["https://github.com/o/r/actions/runs/41"]}))
elif args[:1] == ["api"] and args[-1] == "repos/o/r/issues/5":
    print(json.dumps({"title": "Moral bricht"}))
elif args[:2] == ["repo", "view"]:
    print("main")
elif args[:1] == ["api"]:
    print("{}")
"""


def test_abort_labels_first_then_cancels_and_tells_the_issue(tmp_path, monkeypatch):
    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    result = board.abort("o/r", 5, "factory-state", force_after=0)
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    text = [" ".join(c) for c in calls]
    first_cancel = next(i for i, t in enumerate(text) if t.startswith("run cancel"))
    labelled = next(i for i, t in enumerate(text) if f"labels[]={ABORTED}" in t)
    assert labelled < first_cancel                                       # nothing restarts it while it is cancelled
    assert [t for t in text if t.startswith("run cancel")] == ["run cancel 41 --repo o/r"]
    assert all(any(f"labels/{name.replace(':', '%3A')}" in t for t in text) for name in board.ABORT_REMOVES)
    assert any("comments" in t and "Von Hand abgebrochen" in t for t in text)
    assert result["cancelled"] == ["41"] and "1 Lauf gestoppt" in result["message"]


def test_restart_takes_the_abort_label_away(tmp_path, monkeypatch):
    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    board.restart("o/r", 5)
    text = [" ".join(json.loads(line)) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    removed = next(i for i, t in enumerate(text) if "DELETE" in t and "factory%3Aaborted" in t)
    started = next(i for i, t in enumerate(text) if t.startswith("workflow run"))
    assert removed < started


def test_a_cancelled_run_takes_its_checks_down(tmp_path):
    # A check runs in its own session; the runner's cancel would not reach it.
    proc = subprocess.Popen(["bash", "-c", "sleep 60 & wait"], start_new_session=True)
    checks.GROUPS.add(proc.pid)
    checks.kill_running()
    assert proc.wait(timeout=5) == -signal.SIGKILL and not checks.GROUPS


def test_the_run_ends_its_checks_on_sigterm(tmp_path):
    script = tmp_path / "run.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "from factory import checks, cli\n"
        "cli._end_with_the_run()\n"
        "p = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
        "checks.GROUPS.add(p.pid)\n"
        f"open({str(tmp_path / 'pid')!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = subprocess.Popen([sys.executable, str(script)], cwd=root, env={**os.environ, "PYTHONPATH": root})
    for _ in range(100):
        if (tmp_path / "pid").exists() and (tmp_path / "pid").read_text():
            break
        time.sleep(0.05)
    child = int((tmp_path / "pid").read_text())
    run.send_signal(signal.SIGTERM)
    assert run.wait(timeout=10) == 128 + signal.SIGTERM
    time.sleep(0.2)
    try:
        os.kill(child, 0)
        alive = subprocess.run(["ps", "-o", "stat=", "-p", str(child)], capture_output=True, text=True).stdout.strip()
        assert alive.startswith("Z") or not alive   # gone, or a zombie nobody waits for
    except ProcessLookupError:
        pass
