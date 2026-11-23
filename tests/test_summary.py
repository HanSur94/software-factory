"""Once a ticket is merged, its issue says what was done and how it was proved."""
import json
from types import SimpleNamespace

from conftest import sh
from factory import cli, deliver, hosts, summary


def ticket_ctx(product, n=1):
    return cli.make_ctx(SimpleNamespace(repo=str(product.repo), issue=n, issue_file=str(product.issue), budget=None), local=True)


def comments(product):
    path = product.run_dir / "comments.md"
    return path.read_text() if path.exists() else ""


def dark(product, monkeypatch, extra=""):
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n  delivery:\n    auto_merge: true\n"
                       "    auto_merge_rules: { min_scenarios: 2 }\n" + extra)
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "dark")
    monkeypatch.setattr(hosts.LocalHost, "can_merge", True)
    monkeypatch.setattr(hosts.LocalHost, "merge_pr", lambda self, url, branch, method: None, raising=False)


def test_an_automatic_merge_writes_the_report_once(product, monkeypatch):
    dark(product, monkeypatch)
    cli.main(product.args)
    said = comments(product)
    assert said.count("## Abschlussbericht") == 1 and "Automatisch gemergt:" not in said
    for part in ("**Auftrag:**", "### Abnahmekriterien", "### Was umgesetzt wurde", "### Nachweise",
                 "| Check `", "| Review |", "| Richter |", "**Aufwand:**", "Agent-Läufe"):
        assert part in said, part
    assert "✓ **T1**" in said
    assert product.status()["summary_posted"]
    assert not summary.post(ticket_ctx(product), "factory")          # never twice
    assert comments(product).count("## Abschlussbericht") == 1


def test_a_merge_by_hand_writes_the_report_with_pictures_and_notes(product):
    cli.main(product.args)
    assert product.status()["phase"] == "delivered"
    ctx = ticket_ctx(product)
    (ctx.run_dir / "evidence").mkdir()
    (ctx.run_dir / "evidence" / "01-hud-after.png").write_bytes(b"x")
    (ctx.run_dir / "evidence.json").write_text(json.dumps({"round": 1, "items": [
        {"file": "01-hud-after.png", "title": "hud", "source": "agent", "side": "after", "pair": None}]}))
    (ctx.run_dir / "notes-T1.md").write_text("Added the thing. Could not run the Unity suite here.")
    ctx.host.pr_info = lambda url: {"merge_commit_sha": "abcdef1234", "changed_files": 3, "additions": 40, "deletions": 2}
    deliver.human_outcome(ctx, merged=True)
    said = comments(product)
    assert "von dir gemergt" in said and "Commit `abcdef1`" in said and "3 Dateien, +40 / −2 Zeilen" in said
    assert "### Belege" in said and "01-hud-after.png" in said
    assert "Notizen des Implementierers" in said and "Could not run the Unity suite" in said


def test_switched_off_the_old_line_stays(product, monkeypatch):
    dark(product, monkeypatch, "    closing_summary: false\n")
    cli.main(product.args)
    said = comments(product)
    assert "Automatisch gemergt:" in said and "## Abschlussbericht" not in said


def test_a_closed_pull_request_gets_no_report(product):
    cli.main(product.args)
    deliver.human_outcome(ticket_ctx(product), merged=False)
    assert "## Abschlussbericht" not in comments(product)


def test_the_report_links_the_logs_on_github_and_stays_short_enough():
    ctx = SimpleNamespace(run_dir=None, status={"pr": "https://github.com/o/r/pull/9", "notes": ["x" * 70000]},
                          cfg={"storage": {"branch": "factory-state"}, "pipeline": {"modes": {}}}, ticket=4,
                          host=SimpleNamespace(repo="o/r"), mode=lambda s: "gate")
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        ctx.run_dir = Path(d)
        text = summary.closing(ctx, "human")
    assert "[PR #9](https://github.com/o/r/pull/9) von dir gemergt" in text
    assert len(text) <= summary.LIMIT + 20 and text.endswith("(gekürzt)")
