"""Git helpers. Network operations carry the token themselves, so agents never hold it."""
from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path, label: str, check: bool, env: dict | None = None):
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, env=env, timeout=600)
    if check and proc.returncode != 0:
        raise GitError(f"git {label} failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
    return proc


def git(cwd: Path, *args: str, check: bool = True) -> str:
    return _run(["git", *args], cwd, " ".join(args[:3]), check).stdout.strip()


def git_ok(cwd: Path, *args: str) -> bool:
    return _run(["git", *args], cwd, "", False).returncode == 0


def _auth() -> list[str]:
    token = os.environ.get("FACTORY_GH_TOKEN")
    if not token:
        return []  # local run: the user's own credential helper applies
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]


def git_net(cwd: Path, *args: str, check: bool = True) -> str:
    return _run(["git", *_auth(), *args], cwd, " ".join(args[:3]), check).stdout.strip()


def git_net_ok(cwd: Path, *args: str) -> bool:
    return _run(["git", *_auth(), *args], cwd, "", False).returncode == 0


def git_net_try(cwd: Path, *args: str) -> tuple[bool, str]:
    proc = _run(["git", *_auth(), *args], cwd, "", False)
    return proc.returncode == 0, proc.stderr.strip()[-500:]


def has_remote(cwd: Path) -> bool:
    return bool(git(cwd, "remote", check=False))


def remote_has_branch(cwd: Path, branch: str) -> bool:
    return bool(git_net(cwd, "ls-remote", "--heads", "origin", branch, check=False))


def head(cwd: Path) -> str:
    return git(cwd, "rev-parse", "HEAD")


def is_clean(cwd: Path) -> bool:
    return git(cwd, "status", "--porcelain") == ""


def discard_changes(cwd: Path) -> None:
    git(cwd, "reset", "--hard", "--quiet")
    git(cwd, "clean", "-fd", "--quiet")


def commit_all(cwd: Path, message: str, author: dict) -> bool:
    """Commit everything in the tree. Returns False when there was nothing to commit."""
    git(cwd, "add", "-A")
    if git_ok(cwd, "diff", "--cached", "--quiet"):
        return False
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME=author["name"], GIT_AUTHOR_EMAIL=author["email"],
        GIT_COMMITTER_NAME=author["name"], GIT_COMMITTER_EMAIL=author["email"],
    )
    _run(["git", "commit", "--quiet", "--no-verify", "-m", message], cwd, "commit", True, env)
    return True


def changed_files(cwd: Path, base: str) -> list[str]:
    committed = git(cwd, "diff", "--name-only", f"{base}...HEAD").splitlines()
    status = _run(["git", "status", "--porcelain", "-uall"], cwd, "status", True).stdout
    pending = [line[3:].split(" -> ")[-1].strip('"') for line in status.splitlines()]
    return sorted(set(filter(None, committed + pending)))


def changed_lines(cwd: Path, base: str) -> int:
    total = 0
    for line in git(cwd, "diff", "--numstat", f"{base}...HEAD").splitlines():
        added, removed, _ = line.split("\t", 2)
        if added != "-":  # binary files have no line counts
            total += int(added) + int(removed)
    return total


def merge(cwd: Path, ref: str, author: dict) -> bool:
    """Merge ref into the current branch. On a conflict the tree is left as it was."""
    env = dict(os.environ)
    env.update(GIT_AUTHOR_NAME=author["name"], GIT_AUTHOR_EMAIL=author["email"],
               GIT_COMMITTER_NAME=author["name"], GIT_COMMITTER_EMAIL=author["email"])
    if _run(["git", "merge", "--quiet", "--no-edit", ref], cwd, "merge", False, env).returncode == 0:
        return True
    _run(["git", "merge", "--abort"], cwd, "merge --abort", False)
    return False


def merge_open(cwd: Path, ref: str, author: dict) -> list[str] | None:
    """Merge ref into the current branch, but leave a conflict in the tree for someone to resolve.

    None: merged cleanly, nothing left to do. Otherwise the conflicted files; the merge stays open (MERGE_HEAD), and
    finish_merge or abort_merge ends it."""
    env = dict(os.environ)
    env.update(GIT_AUTHOR_NAME=author["name"], GIT_AUTHOR_EMAIL=author["email"],
               GIT_COMMITTER_NAME=author["name"], GIT_COMMITTER_EMAIL=author["email"])
    if _run(["git", "merge", "--quiet", "--no-edit", ref], cwd, "merge", False, env).returncode == 0:
        return None
    files = git(cwd, "diff", "--name-only", "--diff-filter=U", check=False).splitlines()
    if not files:   # it failed for another reason (an untracked file in the way): nothing anyone could resolve
        abort_merge(cwd)
        raise GitError(f"git merge {ref} failed without a conflict to resolve")
    return files


def conflict_markers(cwd: Path, files: list[str]) -> list[str]:
    """The files that still hold conflict markers."""
    left = []
    for name in files:
        path = Path(cwd) / name
        if path.is_file():
            text = path.read_text(errors="replace")
            if "\n<<<<<<< " in "\n" + text and "\n>>>>>>> " in "\n" + text:
                left.append(name)
    return left


def finish_merge(cwd: Path, message: str, author: dict) -> None:
    """Commit the open merge with everything in the tree, even when the result equals one side."""
    git(cwd, "add", "-A")
    env = dict(os.environ)
    env.update(GIT_AUTHOR_NAME=author["name"], GIT_AUTHOR_EMAIL=author["email"],
               GIT_COMMITTER_NAME=author["name"], GIT_COMMITTER_EMAIL=author["email"])
    _run(["git", "commit", "--quiet", "--no-verify", "-m", message], cwd, "commit", True, env)


def merging(cwd: Path) -> bool:
    """A merge is open in this tree."""
    return git_ok(cwd, "rev-parse", "-q", "--verify", "MERGE_HEAD")


def abort_merge(cwd: Path) -> None:
    _run(["git", "merge", "--abort"], cwd, "merge --abort", False)


def cherry_pick(cwd: Path, sha: str, author: dict) -> bool:
    """Put one commit on top of the current branch. On a conflict the tree is left as it was."""
    env = dict(os.environ)
    env.update(GIT_COMMITTER_NAME=author["name"], GIT_COMMITTER_EMAIL=author["email"])
    if _run(["git", "cherry-pick", sha], cwd, "cherry-pick", False, env).returncode == 0:
        return True
    _run(["git", "cherry-pick", "--abort"], cwd, "cherry-pick --abort", False)
    discard_changes(cwd)
    return False
