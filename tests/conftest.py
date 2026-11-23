import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROFILE = """schema: profile/1
name: tiny
checks:
  - name: readme
    run: "test -f README.md"
  - name: marker
    run: "! grep -q BROKEN *.md"
agent_commands: [python3]
"""


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture(autouse=True)
def defaults_only(monkeypatch, tmp_path):
    """Tests check the documented defaults, whatever the settings page has changed on this machine.
    A judge in a test keeps its session in the test's folder, not in the home folder."""
    monkeypatch.setenv("FACTORY_SETTINGS", "off")
    monkeypatch.setenv("FACTORY_JUDGE_ARCHIVE", str(tmp_path / "judge-archive"))


@pytest.fixture
def product(tmp_path, monkeypatch):
    """A tiny product repository with a profile, plus an issue file and a scenario folder."""
    repo = tmp_path / "product"
    (repo / ".factory").mkdir(parents=True)
    (repo / ".factory" / "profile.yaml").write_text(PROFILE)
    (repo / "README.md").write_text("# tiny\n")
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")

    issue = tmp_path / "issue.md"
    issue.write_text("# Add a thing\n\nPlease add a thing.\n")
    scenarios = tmp_path / "scenarios-repo"
    (scenarios / "scenarios").mkdir(parents=True)
    (scenarios / "scenarios" / "one.md").write_text("A user adds a thing and sees it in the list afterwards.\n")
    (scenarios / "scenarios" / "two.md").write_text("A user removes a thing and it is gone.\n")

    monkeypatch.setenv("FACTORY_FAKE_AGENT", "1")
    monkeypatch.setenv("FACTORY_WORKDIR", str(tmp_path / "work"))
    monkeypatch.delenv("FACTORY_SANDBOXED", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    class Product:
        pass

    p = Product()
    p.repo, p.issue, p.scenarios, p.work = repo, issue, scenarios, tmp_path / "work"
    p.run_dir = p.work / "state" / "tickets" / "1"
    p.args = ["local", "--repo", str(repo), "--issue", "1", "--issue-file", str(issue), "--scenarios", str(scenarios)]
    p.status = lambda: json.loads((p.run_dir / "status.json").read_text())
    p.events = lambda: [json.loads(line) for line in (p.run_dir / "events.jsonl").read_text().splitlines()]
    return p
