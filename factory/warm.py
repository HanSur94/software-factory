"""Folders a project's tools rebuild slowly and git ignores (Unity's Library/).

Every ticket works in a fresh worktree, so without help each one rebuilds them
from nothing: for a Unity project a cold import of several minutes and gigabytes,
once per ticket and once per parallel worker. The profile names them under
warm_dirs. A new worktree starts from a copy of the runner's own checkout, and
after a ticket's checks pass that checkout takes the newer state. On APFS (macOS)
and on filesystems with reflinks the copy is a clone: instant, and it takes no
space until a file changes.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _names(profile: dict) -> list[str]:
    return [d for d in profile.get("warm_dirs") or [] if d and not Path(d).is_absolute() and ".." not in Path(d).parts]


def clone_tree(src: Path, dst: Path) -> None:
    """Copy a directory tree, as a copy-on-write clone where the filesystem can."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    flag = ["-c"] if sys.platform == "darwin" else ["--reflink=auto"]
    if subprocess.run(["cp", *flag, "-R", str(src), str(dst)], capture_output=True).returncode == 0:
        return
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)


def seed(profile: dict, repo: Path, worktree: Path) -> list[str]:
    """Give a new worktree the runner's copy of every warm folder it does not have yet."""
    seeded = []
    for name in _names(profile):
        src, dst = Path(repo) / name, Path(worktree) / name
        if src.is_dir() and not dst.exists():
            clone_tree(src, dst)
            seeded.append(name)
    return seeded


def keep(profile: dict, repo: Path, worktree: Path) -> list[str]:
    """After green checks: the runner's copy takes the worktree's state, so the next ticket starts warm.
    Only on a runner that is kept (self-hosted); a throwaway runner has nobody to hand it to."""
    if os.environ.get("FACTORY_SANDBOXED") == "1":
        return []
    kept = []
    for name in _names(profile):
        src, dst = Path(worktree) / name, Path(repo) / name
        if not src.is_dir():
            continue
        fresh, old = dst.with_name(dst.name + ".factory-new"), dst.with_name(dst.name + ".factory-old")
        shutil.rmtree(fresh, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
        clone_tree(src, fresh)
        if dst.exists():
            dst.rename(old)
        fresh.rename(dst)
        shutil.rmtree(old, ignore_errors=True)
        kept.append(name)
    return kept
