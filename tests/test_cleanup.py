"""Giving back the disk space a project's factory work holds on this machine (factory/cleanup.py)."""
import time

from conftest import sh
from factory import cleanup


def _runner(tmp_path, name="factory-1", busy=False):
    """A runner folder with the project's checkout and two ticket worktrees: one saved on the remote, one not."""
    folder = tmp_path / f"actions-runner-{name}"
    remote = tmp_path / "remote.git"
    if not remote.exists():
        sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(remote))
        seed = tmp_path / "seed"
        sh(tmp_path, "git", "clone", "-q", str(remote), str(seed))
        (seed / "README.md").write_text("x\n")
        sh(seed, "git", "-c", "user.name=t", "-c", "user.email=t@e", "add", "-A")
        sh(seed, "git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "init")
        sh(seed, "git", "push", "-q", "origin", "main")
    checkout = folder / "_work" / "game" / "game"
    checkout.parent.mkdir(parents=True)
    sh(tmp_path, "git", "clone", "-q", str(remote), str(checkout))
    scratch = folder / "_work" / "_temp" / "factory"
    scratch.mkdir(parents=True)
    sh(checkout, "git", "worktree", "add", "-q", "-b", "factory/1-a", str(scratch / "wt-1"))
    sh(scratch / "wt-1", "git", "push", "-q", "origin", "factory/1-a")
    sh(checkout, "git", "fetch", "-q", "origin")
    sh(checkout, "git", "worktree", "add", "-q", "-b", "factory/2-b", str(scratch / "wt-2"))
    (scratch / "wt-2" / "work.txt").write_text("not committed\n")
    (checkout / "judge-out").mkdir()
    return {"name": name, "repo": "o/game", "folder": str(folder), "busy": busy,
            "job": {"label": "Arbeit", "ticket": 3} if busy else None}


def test_the_plan_lists_what_can_go_and_keeps_unsaved_work(tmp_path):
    r = _runner(tmp_path)
    got = cleanup.plan("o/game", [r, {**r, "repo": "o/other", "name": "x"}])
    (row,) = got["runners"]
    kinds = [(i["kind"], i["path"].rsplit("/", 1)[-1]) for i in row["items"]]
    assert ("worktree", "wt-1") in kinds and ("judge", "judge-out") in kinds and kinds[0][0] == "git"
    assert ("worktree", "wt-2") not in kinds and row["kept"] == ["wt-2: hat Änderungen, die nicht committet sind"]


def test_a_busy_runner_is_left_alone(tmp_path):
    r = _runner(tmp_path, busy=True)
    (row,) = cleanup.plan("o/game", [r])["runners"]
    assert row["busy"] and "#3" in row["why_not"] and not any(i["kind"] == "worktree" for i in row["items"])
    c = cleanup.Cleaner()
    assert c.start("o/game", lambda: [r])["ok"]
    for _ in range(100):
        if not c.status("o/game")["running"]:
            break
        time.sleep(0.1)
    st = c.status("o/game")
    assert st["skipped"] and not st["done"]
    assert (tmp_path / "actions-runner-factory-1" / "_work" / "_temp" / "factory" / "wt-1").exists()


def test_cleaning_removes_saved_worktrees_and_repacks(tmp_path):
    r = _runner(tmp_path)
    c = cleanup.Cleaner()
    assert c.start("o/game", lambda: [r])["ok"]
    assert not c.start("o/game", lambda: [r])["ok"]          # one at a time
    for _ in range(300):
        if not c.status("o/game")["running"]:
            break
        time.sleep(0.1)
    st = c.status("o/game")
    scratch = tmp_path / "actions-runner-factory-1" / "_work" / "_temp" / "factory"
    assert not (scratch / "wt-1").exists() and (scratch / "wt-2" / "work.txt").exists()
    assert not (tmp_path / "actions-runner-factory-1" / "_work" / "game" / "game" / "judge-out").exists()
    assert any("Git-Speicher" in line for line in st["done"]) and "error" not in st


def test_tidy_repacks_a_store_split_into_many_packs(tmp_path):
    r = _runner(tmp_path)
    checkout = tmp_path / "actions-runner-factory-1" / "_work" / "game" / "game"
    assert "nichts zu tun" in cleanup.tidy(checkout)
    sh(checkout, "git", "config", "gc.auto", "0")   # what actions/checkout sets
    for i in range(cleanup.TIDY_PACKS + 2):   # many fetches, one pack each
        (checkout / f"f{i}.txt").write_text(str(i))
        sh(checkout, "git", "-c", "user.name=t", "-c", "user.email=t@e", "add", "-A")
        sh(checkout, "git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", str(i))
        sh(checkout, "git", "repack", "-q")
    assert cleanup._git_store(checkout)["packs"] > cleanup.TIDY_PACKS
    assert "neu gepackt zu 1" in cleanup.tidy(checkout)
    assert r["name"] == "factory-1"


def test_a_runner_the_factory_never_used_is_not_offered(tmp_path):
    r = _runner(tmp_path)
    ci = {**r, "name": "ci", "folder": str(tmp_path / "actions-runner-ci")}
    (tmp_path / "actions-runner-ci" / "_work" / "game").mkdir(parents=True)
    assert [row["runner"] for row in cleanup.plan("o/game", [r, ci])["runners"]] == ["factory-1"]


def test_the_inventory_shows_everything_and_offers_only_what_may_go(tmp_path):
    r = _runner(tmp_path)
    (tmp_path / "actions-runner-factory-1" / "_work" / "game" / "game" / ".gitignore").write_text("Library/\n")
    lib = tmp_path / "actions-runner-factory-1" / "_work" / "game" / "game" / "Library"
    lib.mkdir(); (lib / "cache.bin").write_bytes(b"x" * (2 * 2**20))
    (row,) = cleanup.inventory("o/game", [r])["runners"]
    by = {i["text"]: i for i in row["items"]}
    assert by["Worktree für #1"]["removable"] and not by["Worktree für #2"]["removable"]
    assert "nicht committet" in by["Worktree für #2"]["why"]
    assert not by["Cache Library"]["removable"] and not by["Arbeitskopie des Projekts"]["removable"]
    busy = cleanup.inventory("o/game", [{**r, "busy": True, "job": {"label": "Arbeit", "ticket": 1}}])["runners"][0]
    assert busy["removable"] == 0 and any(i["why"] == "in Benutzung" for i in busy["items"])


def test_a_cleanup_can_take_just_one_runner(tmp_path):
    a = _runner(tmp_path, "factory-1")
    b = _runner(tmp_path, "factory-2")
    cleaner = cleanup.Cleaner()
    cleaner.start("o/game", lambda: [a, b], only="factory-2")
    for _ in range(300):
        if not cleaner.status("o/game")["running"]:
            break
        time.sleep(0.1)
    assert (tmp_path / "actions-runner-factory-1" / "_work" / "_temp" / "factory" / "wt-1").exists()
    assert not (tmp_path / "actions-runner-factory-2" / "_work" / "_temp" / "factory" / "wt-1").exists()


def test_the_project_ci_runner_is_offered_a_repack(tmp_path):
    r = _runner(tmp_path)
    ci = {**r, "name": "ci", "folder": str(tmp_path / "actions-runner-ci")}
    checkout = tmp_path / "actions-runner-ci" / "_work" / "game" / "game"
    checkout.parent.mkdir(parents=True)
    sh(tmp_path, "git", "clone", "-q", str(tmp_path / "remote.git"), str(checkout))
    rows = {row["runner"]: row for row in cleanup.plan("o/game", [r, ci])["runners"]}
    assert [i["kind"] for i in rows["ci"]["items"]] == ["git"]
