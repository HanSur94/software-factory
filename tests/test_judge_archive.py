"""The judge keeps its session on its own machine, and the board opens it once the ticket is done."""

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from factory import board, judge


def ctx(tmp_path, keep=True):
    return SimpleNamespace(ticket=42, cfg={"verify": {"keep_judge_sessions": keep}})


def judged(tmp_path):
    out = tmp_path / "judge-out"
    private = out / "private"
    (private / "logs").mkdir(parents=True)
    (private / "sessions").mkdir()
    (private / "logs" / "01-judge.md").write_text("model: claude-opus\ncost_usd: 0.1\nduration_s: 12\nstate: ok\n\nAlles erfüllt.")
    (private / "sessions" / "01-judge.jsonl").write_text(json.dumps({"type": "text", "text": "prüfe"}) + "\n")
    (private / "events.jsonl").write_text(json.dumps({"ts": "2026-11-19T10:00:00Z", "station": "judge", "kind": "agent", "seq": 1}) + "\n")
    scen = tmp_path / "scenarios" / "scenarios"
    scen.mkdir(parents=True)
    (scen / "01.md").write_text("# Die Armee bricht bei Moral null\n")
    (private / "manifest.json").write_text(json.dumps([{"id": "Sabc", "path": str(scen / "01.md")}]))
    (private / "judge-result.json").write_text(json.dumps({"results": [{"id": "Sabc", "satisfied": True}]}))
    (out / "verdict.json").write_text(json.dumps({"scenarios_total": 1, "scenarios_passed": 1, "passed": True}))
    return out, [scen / "01.md"], tmp_path / "scenarios"


def test_the_session_is_kept_on_this_machine_and_read_by_the_board(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_JUDGE_ARCHIVE", str(tmp_path / "archive"))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    out, files, root = judged(tmp_path)
    kept = judge.keep_session(ctx(tmp_path), out, files, root)
    assert kept.parent == tmp_path / "archive" / "o__r" / "42"
    assert (kept / "scenarios" / "01-Sabc.md").read_text().endswith("Moral null\n")
    assert oct((tmp_path / "archive").stat().st_mode)[-3:] == "700"
    got = board.judge_session("o/r", 42)["rounds"]
    assert len(got) == 1 and got[0]["verdict"]["passed"] is True
    assert got[0]["runs"][0]["station"] == "judge" and got[0]["runs"][0]["final"] == "Alles erfüllt."
    assert got[0]["scenarios"][0]["name"] == "01-Sabc.md"


def test_off_or_outside_github_nothing_is_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_JUDGE_ARCHIVE", str(tmp_path / "archive"))
    out, files, root = judged(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert judge.keep_session(ctx(tmp_path, keep=False), out, files, root) is None
    monkeypatch.delenv("GITHUB_REPOSITORY")
    assert judge.keep_session(ctx(tmp_path), out, files, root) is None
    assert not (tmp_path / "archive").exists()


def test_only_the_newest_rounds_and_recent_tickets_stay(tmp_path):
    repo = tmp_path / "o__r"
    for n in range(7):
        (repo / "1" / f"2026111{n}T000000Z").mkdir(parents=True)
    old = repo / "2" / "20260101T000000Z"
    old.mkdir(parents=True)
    past = time.time() - 90 * 86400
    os.utime(old, (past, past))
    judge.prune(repo)
    assert sorted(p.name for p in (repo / "1").iterdir()) == [f"2026111{n}T000000Z" for n in range(2, 7)]
    assert not (repo / "2").exists()


def test_no_archive_means_no_rounds(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_JUDGE_ARCHIVE", str(tmp_path / "none"))
    assert board.judge_session("o/r", 1) == {"rounds": []}


def test_a_running_judge_is_shown_live_then_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_JUDGE_ARCHIVE", str(tmp_path / "archive"))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    out, files, root = judged(tmp_path)
    (out / "verdict.json").unlink()                       # still judging
    target = judge.open_round(ctx(tmp_path), out)
    assert (target / judge.LIVE).is_file()
    got = board.judge_session("o/r", 42)["rounds"]
    assert got[0]["live"] and got[0]["state"] == "live" and got[0]["verdict"] is None
    assert got[0]["runs"][0]["station"] == "judge"         # read from the judge's private folder as it works
    assert got[0]["scenarios"][0]["name"] == "01-Sabc.md" and "Moral null" in got[0]["scenarios"][0]["text"]
    (out / "verdict.json").write_text(json.dumps({"scenarios_total": 1, "scenarios_passed": 1, "passed": True}))
    kept = judge.keep_session(ctx(tmp_path), out, files, root, target=target)
    assert kept == target and not (target / judge.LIVE).exists()
    got = board.judge_session("o/r", 42)["rounds"]
    assert len(got) == 1 and got[0]["state"] == "done" and got[0]["verdict"]["passed"] is True


def test_a_judge_that_died_shows_a_broken_round(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_JUDGE_ARCHIVE", str(tmp_path / "archive"))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    out, _, _ = judged(tmp_path)
    target = judge.open_round(ctx(tmp_path), out)
    pointer = json.loads((target / judge.LIVE).read_text())
    (target / judge.LIVE).write_text(json.dumps({**pointer, "pid": 99999999}))   # no such process
    got = board.judge_session("o/r", 42)["rounds"]
    assert got[0]["state"] == "broken" and not got[0]["live"] and got[0]["runs"] == []


def test_the_board_answers_for_a_ticket_that_still_runs():
    import inspect
    src = inspect.getsource(board.serve)
    assert '"locked"' not in src   # the owner sees the judge at any time, not only once the ticket is done


def test_every_judge_run_starts_from_an_empty_folder(tmp_path, monkeypatch):
    # The runner keeps its workspace: another ticket's logs and verdict must not become this ticket's.
    out = tmp_path / "judge-out"
    (out / "private" / "logs").mkdir(parents=True)
    (out / "private" / "logs" / "01-judge.md").write_text("ticket 99")
    (out / "verdict.json").write_text(json.dumps({"ticket": 99}))
    seen = {}

    def fake_judge(ctx, product, scenarios, out_, seen_):
        seen["left"] = sorted(p.name for p in out_.rglob("*"))
        return {"ticket": 42}
    monkeypatch.setattr(judge, "_judge", fake_judge)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert judge.run_judge(ctx(tmp_path), tmp_path, None, out) == {"ticket": 42}
    assert seen["left"] == []


def test_the_judge_is_told_which_checks_already_passed(tmp_path, monkeypatch):
    from factory import stations
    got = {}
    monkeypatch.setattr(stations, "run_station", lambda ctx, role, template, values, *a, **k: got.update(values) or (None, {}))
    c = SimpleNamespace(profile={"checks": [{"name": "suite", "run": "tools/ci/test.sh --fast"}]},
                        cfg={"roles": {"judge": {"prompt": "stations/judge.md"}}})
    stations.judge(c, tmp_path, tmp_path / "m.json", tmp_path / "r.json", tmp_path)
    assert got["checks_passed"] == "- `tools/ci/test.sh --fast` (suite)"
    text = (Path(__file__).resolve().parents[1] / "stations" / "judge.md").read_text()
    assert "{{checks_passed}}" in text and "Do not run these checks again" in text
