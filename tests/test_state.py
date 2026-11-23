"""The state branch, written by two tickets at once on runners without a git identity."""
from conftest import sh
from factory.state import BranchState

AUTHOR = {"name": "software-factory", "email": "software-factory@users.noreply.github.com"}


def test_two_tickets_publish_at_once_without_a_git_identity(tmp_path, monkeypatch):
    # A GitHub-hosted runner: no user.name, no user.email anywhere.
    # useConfigOnly stops git from guessing one from the host name, as it cannot on a runner either.
    (tmp_path / "gitconfig").write_text("[user]\n\tuseConfigOnly = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(k, raising=False)
    remote = tmp_path / "remote.git"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    sh(tmp_path, "git", "clone", "-q", str(remote), str(seed))
    (seed / "README.md").write_text("x\n")
    sh(seed, "git", "add", "-A")
    sh(seed, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    sh(seed, "git", "push", "-q", "origin", "HEAD:refs/heads/main")

    runners = []
    for n in (1, 2):
        repo = tmp_path / f"runner{n}"
        sh(tmp_path, "git", "clone", "-q", str(remote), str(repo))
        runners.append(BranchState(repo, "factory-state", tmp_path / f"work{n}", AUTHOR))

    first = runners[0].open(1)                    # creates the branch
    (first / "status.json").write_text("{}\n")
    runners[0].save("ticket 1 started")
    second = runners[1].open(2)                   # checks out the branch as it is now
    (first / "events.jsonl").write_text("a\n")
    runners[0].save("ticket 1 moved on")          # the branch moves under the second runner
    (second / "status.json").write_text("{}\n")
    runners[1].save("ticket 2 started")           # rejected push, rebase, push again

    files = sh(remote, "git", "ls-tree", "-r", "--name-only", "factory-state").splitlines()
    assert {"tickets/1/status.json", "tickets/1/events.jsonl", "tickets/2/status.json"} <= set(files)
