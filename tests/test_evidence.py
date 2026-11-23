"""Pictures as evidence: collected after green checks, kept on the state branch, shown and looked at."""

import json
import struct
import subprocess
import zlib
from pathlib import Path
from types import SimpleNamespace

from factory import board, evidence
from factory.context import Ctx, new_status


def png(path: Path, w: int = 8, h: int = 6, shade: int = 0) -> Path:
    """A tiny valid PNG, so the tests need no image library."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\0" + bytes([shade, 100, 200]) * w for _ in range(h))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_ctx(tmp_path, profile=None, **conf):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
    run_dir = tmp_path / "state" / "tickets" / "7"
    run_dir.mkdir(parents=True)
    status = new_status(7)
    status["base_sha"] = git(repo, "rev-parse", "HEAD")
    cfg = {"evidence": {**evidence.DEFAULTS, **conf}, "storage": {"branch": "factory-state"}, "agent": {}}
    ctx = Ctx(cfg=cfg, profile={"checks": [], **(profile or {})}, repo=repo, workdir=tmp_path / "work", ticket=7,
              run_dir=run_dir, status=status, host=SimpleNamespace(repo="o/r"))
    return ctx, repo


def test_before_and_after_names_make_a_pair():
    assert evidence._pair("before-hud") == ("before", "hud")
    assert evidence._pair("minimap_after") == ("after", "minimap")
    assert evidence._pair("Vorher-Karte") == ("before", "Karte")
    assert evidence._pair("overview") == (None, "overview")


def test_pictures_of_capture_agent_and_repository_land_on_the_state_branch(tmp_path):
    ctx, repo = make_ctx(tmp_path)
    inbox = evidence.inbox(ctx)
    png(inbox / "before-hud.png"), png(inbox / "after-hud.png", shade=90), png(inbox / "notes.txt").rename(inbox / "notes.txt")
    png(repo / "docs" / "evidence" / "walk.png")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change")
    shots = {"before": png(tmp_path / "cap" / "b" / "field.png").parent, "after": png(tmp_path / "cap" / "a" / "field.png").parent}

    record = evidence.collect(ctx, repo, shots)

    items = record["items"]
    assert [(i["source"], i["side"]) for i in items] == [
        ("capture", "before"), ("capture", "after"), ("agent", "before"), ("agent", "after"), ("repo", None)]
    assert items[0]["pair"] == items[1]["pair"] == "capture:field" and items[2]["pair"] == "agent:hud"
    assert items[4]["path"] == "docs/evidence/walk.png"
    assert all((ctx.run_dir / "evidence" / i["file"]).is_file() and len(i["hash"]) == 12 for i in items)
    assert json.loads((ctx.run_dir / "evidence.json").read_text())["items"] == items
    assert any(json.loads(line)["station"] == "evidence" for line in (ctx.run_dir / "events.jsonl").read_text().splitlines())


def test_without_a_capture_command_only_the_tickets_own_pictures_count(tmp_path, monkeypatch):
    # The runner starts the factory inside the product checkout: its icons and textures are not evidence.
    ctx, repo = make_ctx(tmp_path)
    png(repo / "Packages" / "ui" / ".icon.png")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "packages")
    ctx.status["base_sha"] = git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    png(evidence.inbox(ctx) / "after-hud.png")
    for captured in (None, {}, {"before": None, "after": ""}):
        record = evidence.collect(ctx, repo, captured)
        assert [(i["source"], i["title"]) for i in record["items"]] == [("agent", "hud")] and record["skipped"] == 0


def test_only_so_many_pictures_and_a_new_round_replaces_the_last(tmp_path):
    ctx, repo = make_ctx(tmp_path, max_images=2)
    for n in range(4):
        png(evidence.inbox(ctx) / f"shot{n}.png")
    record = evidence.collect(ctx, repo)
    assert len(record["items"]) == 2 and record["skipped"] == 2
    for p in evidence.inbox(ctx).iterdir():
        p.unlink()
    assert evidence.collect(ctx, repo) == {}
    assert not (ctx.run_dir / "evidence.json").exists() and not (ctx.run_dir / "evidence").exists()


def test_the_implementer_is_told_where_to_save_and_the_reviewer_what_to_look_at(tmp_path):
    ctx, repo = make_ctx(tmp_path)
    writer = evidence.note(ctx, "implementer")
    assert str(evidence.inbox(ctx)) in writer and "before-<what>.png" in writer
    assert evidence.dirs(ctx, "implementer") == [evidence.inbox(ctx)] and evidence.dirs(ctx, "reviewer") == []
    assert evidence.env(ctx, "implementer") == {"FACTORY_EVIDENCE_DIR": str(evidence.inbox(ctx))}
    assert evidence.note(ctx, "reviewer") == ""                       # nothing collected yet
    png(evidence.inbox(ctx) / "after-menu.png")
    evidence.collect(ctx, repo)
    seen = evidence.note(ctx, "reviewer")
    assert "Read tool" in seen and str(ctx.run_dir / "evidence" / "01-menu-after.png") in seen
    ctx.cfg["evidence"]["reviewer_sees"] = False
    assert evidence.note(ctx, "reviewer") == "" and evidence.note(ctx, "judge") != ""
    ctx.cfg["evidence"]["enabled"] = False
    assert evidence.note(ctx, "implementer") == "" and evidence.gather(ctx, repo) == {}


def test_the_pull_request_shows_pairs_side_by_side(tmp_path):
    ctx, repo = make_ctx(tmp_path)
    png(evidence.inbox(ctx) / "before-hud.png"), png(evidence.inbox(ctx) / "after-hud.png"), png(evidence.inbox(ctx) / "map.png")
    evidence.collect(ctx, repo)
    text = "\n".join(evidence.pr_section(ctx))
    assert "| hud: vorher | nachher |" in text
    assert "https://github.com/o/r/blob/factory-state/tickets/7/evidence/" in text and text.count("<img") == 3
    assert "*map* · vom Implementierer" in text


def test_the_capture_command_shows_base_and_change_and_runs_the_base_once(tmp_path):
    script = "python3 -c \"import os,shutil; shutil.copy(os.environ['SRC'], os.path.join(os.environ['FACTORY_EVIDENCE_OUT'], 'field.png'))\""
    ctx, repo = make_ctx(tmp_path, profile={"evidence": {"capture": f"echo run >> {tmp_path}/runs; " + script},
                                            "env": {"SRC": str(png(tmp_path / "src.png"))}})
    got = evidence.capture(ctx, repo)
    assert set(got) == {"before", "after"} and (got["before"] / "field.png").is_file()
    evidence.capture(ctx, repo)
    assert (tmp_path / "runs").read_text().count("run") == 3            # after, before, after: the base only once
    assert "base-checkout" not in git(repo, "worktree", "list")
    record = evidence.collect(ctx, repo, got)
    assert [i["pair"] for i in record["items"]] == ["capture:field", "capture:field"]


def test_a_broken_capture_is_a_note_not_a_stop(tmp_path):
    ctx, repo = make_ctx(tmp_path, profile={"evidence": {"capture": "exit 3", "before": False}})
    assert evidence.gather(ctx, repo) == {}
    assert ctx.status["notes"] == []      # a failed command only leaves no pictures and its log
    assert (ctx.run_dir / "checks" / "capture-after.log").exists()


def test_the_judge_gets_a_copy_in_its_private_folder(tmp_path):
    ctx, repo = make_ctx(tmp_path)
    png(evidence.inbox(ctx) / "after-hud.png")
    evidence.collect(ctx, repo)
    private = tmp_path / "judge" / "private"
    record = evidence.for_judge(ctx, repo, private, source=ctx.run_dir)
    assert record["items"][0]["file"] in [p.name for p in (private / "evidence").iterdir()]
    ctx.run_dir = private   # as in the judge job
    assert str(private / "evidence") in evidence.note(ctx, "judge")


def test_the_board_names_only_pictures_it_may_serve():
    text = json.dumps({"round": 1, "items": [{"file": "01-a.png", "title": "a", "hash": "x"},
                                             {"file": "../secret.png"}, {"file": "02-b.txt"}]})
    got = board.evidence_of(text)
    assert [i["file"] for i in got["items"]] == ["01-a.png"]
    assert board.evidence_of(json.dumps({"items": [{"file": "../x.png"}]})) is None and board.evidence_of(None) is None
    card = board.card("o/r", 7, {"evidence.json": text}, None, None)
    assert card["evidence"]["items"][0]["file"] == "01-a.png"
