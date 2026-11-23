"""Where ticket state lives between runs. Two interchangeable stores."""
from __future__ import annotations

from pathlib import Path

from .gitops import GitError, commit_all, git, git_net, git_net_try, git_ok, remote_has_branch


class LocalState:
    """A plain directory. Used for local runs."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def open(self, ticket: int) -> Path:
        d = self.root / "tickets" / str(ticket)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def tickets_dir(self) -> Path:
        return self.root / "tickets"

    def save(self, message: str) -> None:
        pass


class BranchState:
    """Files on an orphan branch of the product repository (storage.state: repo-files).

    Survives runners, gives an audit trail, needs no database. Parallel tickets
    write to different folders, so a rejected push is fixed by rebasing.
    """

    def __init__(self, repo: Path, branch: str, workdir: Path, author: dict):
        self.repo, self.branch, self.author = Path(repo), branch, author
        self.wt = Path(workdir) / "state"

    def _checkout(self) -> None:
        if (self.wt / ".git").exists():
            return
        git(self.repo, "worktree", "prune")
        if remote_has_branch(self.repo, self.branch):
            git_net(self.repo, "fetch", "--quiet", "origin", f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}")
            git(self.repo, "worktree", "add", "--quiet", "-B", self.branch, str(self.wt), f"origin/{self.branch}")
        else:
            git(self.repo, "worktree", "add", "--quiet", "--detach", str(self.wt))
            git(self.wt, "checkout", "--quiet", "--orphan", self.branch)
            git(self.wt, "rm", "-rf", "--quiet", ".", check=False)
            (self.wt / "README.md").write_text(
                "Zustand der Software Factory: ein Ordner je Ticket mit Vertragsdateien und Ereignissen.\n"
                "Dieser Branch wird von der Factory geschrieben.\n")

    def open(self, ticket: int) -> Path:
        self._checkout()
        d = self.wt / "tickets" / str(ticket)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def tickets_dir(self) -> Path:
        self._checkout()
        return self.wt / "tickets"

    def _abort_rebase(self) -> None:
        """A rebase cut off earlier (a failed save) blocks every later one."""
        if not (self.wt / ".git").exists():
            return
        for name in ("rebase-merge", "rebase-apply"):
            path = Path(git(self.wt, "rev-parse", "--git-path", name, check=False) or name)
            if (path if path.is_absolute() else self.wt / path).exists():
                git(self.wt, "rebase", "--abort", check=False)
                return

    def save(self, message: str) -> None:
        self._abort_rebase()
        if not commit_all(self.wt, f"state {message}", self.author):
            return
        error = ""
        # Rebasing rewrites commits, so git wants a committer. A GitHub-hosted runner has none configured.
        who = ["-c", f"user.name={self.author['name']}", "-c", f"user.email={self.author['email']}"]
        for _ in range(5):
            ok, error = git_net_try(self.wt, "push", "--quiet", "origin", f"HEAD:refs/heads/{self.branch}")
            if ok:
                return
            if not remote_has_branch(self.repo, self.branch):
                break  # not a race with another ticket: retrying will not help
            git_net(self.wt, "fetch", "--quiet", "origin", f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}")
            if not git_ok(self.wt, *who, "rebase", "--quiet", f"origin/{self.branch}"):
                git(self.wt, "rebase", "--abort", check=False)
                error = f"rebase onto origin/{self.branch} failed"
        raise GitError(f"could not push state to {self.branch}: {error}")
