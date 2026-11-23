"""Warm folders: a ticket worktree starts from the runner's copy, and green checks hand it back."""
import json

from conftest import sh
from factory import cli, warm


def test_clone_tree_copies_and_keeps_links(tmp_path):
    src = tmp_path / "Library"
    (src / "Artifacts").mkdir(parents=True)
    (src / "Artifacts" / "a.bin").write_bytes(b"x" * 1000)
    (src / "link").symlink_to("Artifacts/a.bin")
    warm.clone_tree(src, tmp_path / "copy" / "Library")
    assert (tmp_path / "copy" / "Library" / "Artifacts" / "a.bin").read_bytes() == b"x" * 1000
    assert (tmp_path / "copy" / "Library" / "link").is_symlink()


def test_seed_and_keep(tmp_path, monkeypatch):
    monkeypatch.delenv("FACTORY_SANDBOXED", raising=False)
    repo, wt = tmp_path / "repo", tmp_path / "wt"
    (repo / "Library").mkdir(parents=True)
    (repo / "Library" / "old.txt").write_text("runner\n")
    wt.mkdir()
    profile = {"warm_dirs": ["Library", "../escape", "/abs"]}
    assert warm.seed(profile, repo, wt) == ["Library"]
    assert (wt / "Library" / "old.txt").read_text() == "runner\n"
    assert warm.seed(profile, repo, wt) == []                       # never over a folder the worktree has
    (wt / "Library" / "new.txt").write_text("ticket\n")
    assert warm.keep(profile, repo, wt) == ["Library"]
    assert (repo / "Library" / "new.txt").exists() and not (repo / "Library.factory-old").exists()
    monkeypatch.setenv("FACTORY_SANDBOXED", "1")
    assert warm.keep(profile, repo, wt) == []                       # a throwaway runner keeps nothing


def test_ticket_worktree_starts_warm_and_hands_back(product):
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "warm_dirs: [Library]\n")
    (product.repo / ".gitignore").write_text("Library/\n")
    sh(product.repo, "git", "add", "-A")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "warm")
    (product.repo / "Library").mkdir()
    (product.repo / "Library" / "import.cache").write_text("warm\n")
    assert cli.main(product.args) == 0
    wt = product.work / "wt-1"
    assert (wt / "Library" / "import.cache").read_text() == "warm\n"
    assert json.loads((product.run_dir / "status.json").read_text())["phase"] == "delivered"
