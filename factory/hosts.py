"""Ticket system and code host. Two interchangeable adapters: GitHub and local files."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from . import events
from .gitops import git_net

MARK = "<!-- software-factory -->"  # tells factory comments from the owner's


class HostError(RuntimeError):
    pass


class GitHubHost:
    remote = True

    def __init__(self, repo_dir: Path, ticket: int):
        self.ticket = ticket
        self.repo = os.environ.get("GITHUB_REPOSITORY") or self._gh("repo", "view", "--json", "nameWithOwner",
                                                                     "--jq", ".nameWithOwner", cwd=repo_dir)
        self.owner = self.repo.split("/")[0]

    def _gh(self, *args: str, stdin: str | None = None, cwd: Path | None = None, check: bool = True) -> str:
        env = dict(os.environ)
        if env.get("FACTORY_GH_TOKEN"):
            env["GH_TOKEN"] = env["FACTORY_GH_TOKEN"]
        proc = subprocess.run(["gh", *args], input=stdin, text=True, capture_output=True, env=env,
                              cwd=str(cwd) if cwd else None, timeout=120)
        if check and proc.returncode != 0:
            raise HostError(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()[-400:]}")
        return proc.stdout.strip()

    def _api(self, method: str, path: str, body: dict | None = None, check: bool = True):
        args = ["api", "-X", method, f"repos/{self.repo}/{path}"]
        out = self._gh(*args, "--input", "-", stdin=json.dumps(body), check=check) if body is not None \
            else self._gh(*args, check=check)
        return json.loads(out) if out.startswith(("{", "[")) else None

    def default_branch(self) -> str:
        return self._gh("api", f"repos/{self.repo}", "--jq", ".default_branch")

    def issue(self) -> dict:
        raw = self._api("GET", f"issues/{self.ticket}")
        comments = self._api("GET", f"issues/{self.ticket}/comments?per_page=100") or []
        return {
            "number": self.ticket, "title": raw["title"], "body": raw.get("body") or "",
            "labels": [label["name"] for label in raw.get("labels", [])],
            "comments": [{
                "body": c.get("body") or "", "created_at": c["created_at"],
                "by_owner": c["user"]["login"] == self.owner and MARK not in (c.get("body") or ""),
            } for c in comments],
        }

    def current_labels(self) -> list[str]:
        """The issue's labels right now: the owner may have aborted the ticket while this run works."""
        raw = self._api("GET", f"issues/{self.ticket}", check=False) or {}
        return [label["name"] for label in raw.get("labels", [])]

    def comment(self, body: str) -> str:
        created = self._api("POST", f"issues/{self.ticket}/comments", {"body": f"{body}\n\n{MARK}"})
        return created["created_at"]

    def labels(self, add: list[str] = (), remove: list[str] = ()) -> None:
        self.labels_on(self.ticket, add, remove)

    def labels_on(self, number: int, add: list[str] = (), remove: list[str] = ()) -> None:
        for name in remove:
            self._api("DELETE", f"issues/{number}/labels/{quote(name, safe='')}", check=False)
        if add:
            self._api("POST", f"issues/{number}/labels", {"labels": list(add)})

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        return int(self._api("POST", "issues", {"title": title, "body": f"{body}\n\n{MARK}", "labels": list(labels)})["number"])

    def issue_info(self, number: int) -> dict:
        raw = self._api("GET", f"issues/{number}")
        return {"number": number, "state": raw["state"], "body": raw.get("body") or "",
                "labels": [label["name"] for label in raw.get("labels", [])]}

    def close_issue_number(self, number: int) -> None:
        self._api("PATCH", f"issues/{number}", {"state": "closed", "state_reason": "completed"}, check=False)

    def push(self, worktree: Path, branch: str) -> None:
        git_net(worktree, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")

    def open_pr(self, branch: str, base: str, title: str, body: str) -> str:
        existing = self._gh("pr", "list", "--repo", self.repo, "--head", branch, "--state", "open",
                            "--json", "number,url")
        found = json.loads(existing or "[]")
        if found:
            self._api("PATCH", f"pulls/{found[0]['number']}", {"title": title, "body": body})
            return found[0]["url"]
        created = self._api("POST", "pulls", {"title": title, "head": branch, "base": base, "body": body})
        return created["html_url"]

    can_merge = True

    def merge_pr(self, url: str, branch: str, method: str) -> str | None:
        """Merge the pull request. Returns None on success, otherwise GitHub's reason."""
        number = url.rstrip("/").rsplit("/", 1)[-1]
        env = dict(os.environ)
        if env.get("FACTORY_GH_TOKEN"):
            env["GH_TOKEN"] = env["FACTORY_GH_TOKEN"]
        error = ""
        for _ in range(4):  # right after a push GitHub may still be computing mergeability
            proc = subprocess.run(["gh", "api", "-X", "PUT", f"repos/{self.repo}/pulls/{number}/merge",
                                   "-f", f"merge_method={method}"], text=True, capture_output=True, env=env, timeout=120)
            if proc.returncode == 0:
                self._api("DELETE", f"git/refs/heads/{branch}", check=False)
                return None
            error = (proc.stdout + proc.stderr).strip()[-300:]
            time.sleep(5)
        return error

    def close_issue(self) -> None:
        self._api("PATCH", f"issues/{self.ticket}", {"state": "closed", "state_reason": "completed"}, check=False)

    @staticmethod
    def caller_workflow() -> str:
        ref = os.environ.get("FACTORY_CALLER_WORKFLOW", "")  # owner/repo/.github/workflows/x.yml@refs/heads/main
        return ref.split("@")[0].rsplit("/", 1)[-1] or "factory.yml"

    def dispatch(self, issue: int | None = None, dry_run: bool = False) -> None:
        """Start the next round. workflow_dispatch is the one event the job token may trigger."""
        self._gh("workflow", "run", self.caller_workflow(), "--repo", self.repo, "--ref", self.default_branch(),
                 "-f", f"issue={issue or self.ticket}", *(["-f", "dry_run=true"] if dry_run else []))

    def open_issues(self, label: str) -> list[dict]:
        """Open issues with this label, oldest first: number and creation time."""
        found = self._api("GET", f"issues?state=open&per_page=100&sort=created&direction=asc&labels={quote(label, safe='')}",
                          check=False) or []
        return [{"number": i["number"], "created_at": i["created_at"]} for i in found if "pull_request" not in i]

    def active_runs(self) -> list[dict]:
        """Runs of the factory workflow that are not finished: id and creation time. The run asking is left out:
        a sweep is itself a run of that workflow, newer than every ticket's last event, and counted as one that
        might belong to a ticket it made the orchestrator wait on every ticket that broke off."""
        out = self._gh("run", "list", "--repo", self.repo, "--workflow", self.caller_workflow(), "--limit", "100",
                       "--json", "databaseId,status,createdAt", check=False)
        own = os.environ.get("GITHUB_RUN_ID", "")
        return [{"id": str(r["databaseId"]), "created_at": r["createdAt"]} for r in json.loads(out or "[]")
                if r["status"] not in ("completed",) and str(r["databaseId"]) != own]

    def pr_info(self, url: str) -> dict:
        """Size and merge commit of a pull request, for the closing report."""
        pr = self._api("GET", f"pulls/{url.rstrip('/').rsplit('/', 1)[-1]}", check=False) or {}
        return {k: pr.get(k) for k in ("merge_commit_sha", "changed_files", "additions", "deletions") if pr.get(k) is not None}

    def pull_request(self, url: str) -> dict | None:
        """The head commit of an open pull request and whether GitHub sees a merge conflict with its base, in one
        call. None when the pull request is no longer open."""
        number = url.rstrip("/").rsplit("/", 1)[-1]
        pr = self._api("GET", f"pulls/{number}", check=False) or {}
        if pr.get("state") != "open":
            return None
        # mergeable_state "dirty" is GitHub's word for a conflict; while it computes, mergeable is null.
        return {"head": (pr.get("head") or {}).get("sha"), "conflict": pr.get("mergeable_state") == "dirty"}

    def failed_check(self, sha: str) -> tuple[str, str] | None:
        """(check name, workflow run id) of a red check on this commit that no run of the same name made up for."""
        runs = (self._api("GET", f"commits/{sha}/check-runs?per_page=100", check=False) or {}).get("check_runs") or []
        green = {r["name"] for r in runs if r.get("conclusion") in ("success", "neutral", "skipped")}
        for r in runs:
            match = re.search(r"/actions/runs/(\d+)", r.get("details_url") or "")
            if r.get("conclusion") in ("failure", "timed_out") and r["name"] not in green and match:
                return r["name"], match.group(1)
        return None

    def run_workflow(self, workflow: str, ref: str, inputs: dict | None = None) -> None:
        """Start one of the project's own workflows on a branch (it needs a workflow_dispatch trigger)."""
        fields = [x for key, value in (inputs or {}).items() for x in ("-f", f"{key}={value}")]
        self._gh("workflow", "run", workflow, "--repo", self.repo, "--ref", ref, *fields)

    def approve_waiting_runs(self, branch: str, sha: str, wait_s: int = 45) -> list[str]:
        """Release the project's own workflows that GitHub holds on a pull request the factory opened.

        A pull request opened with the job token gets its pull_request workflows only as runs that wait for an
        approval (action_required). Until someone approves them the checks the branch protection requires
        never report on the pull request, and it stays blocked. Waits a little for the runs to appear;
        returns the names of the workflows it released."""
        deadline, released = time.time() + wait_s, []
        while True:
            out = self._gh("run", "list", "--repo", self.repo, "--branch", branch, "--limit", "30", "--json",
                           "databaseId,conclusion,event,headSha,workflowName", check=False)
            waiting = [r for r in json.loads(out or "[]") if r.get("conclusion") == "action_required"
                       and r.get("headSha") == sha and r.get("event") == "pull_request"]
            for run in waiting:
                if self._api("POST", f"actions/runs/{run['databaseId']}/approve", {}, check=False) is not None:
                    released.append(run["workflowName"])
            if released or time.time() >= deadline:
                return released
            time.sleep(10)

    def branch_head(self, branch: str) -> str | None:
        ref = self._api("GET", f"git/ref/heads/{quote(branch, safe='/')}", check=False)
        return ((ref or {}).get("object") or {}).get("sha")

    def failed_log(self, run_id: str, limit: int = 12000, wait_s: int = 300) -> str:
        """The log of the failed steps of a workflow run, newest part kept. A result reported by the run's own
        last job (report-ci.yml) arrives before the run has ended, and GitHub gives no log until it has."""
        deadline = time.time() + wait_s
        while time.time() < deadline and self._gh("run", "view", str(run_id), "--repo", self.repo, "--json", "status",
                                                  "--jq", ".status", check=False) not in ("completed", ""):
            time.sleep(10)
        text = self._gh("run", "view", str(run_id), "--repo", self.repo, "--log-failed", check=False)
        return text[-limit:]

    def run_url(self, run_id: str) -> str:
        return f"https://github.com/{self.repo}/actions/runs/{run_id}"

    def checks_state(self, sha: str | None) -> str:
        """green, pending or red for the checks on a commit. A check counts once, by name: a run of it that passed
        is enough, so a duplicate that was cancelled or waits for approval does not hold the merge back."""
        if not sha:
            return "pending"
        runs = (self._api("GET", f"commits/{sha}/check-runs?per_page=100", check=False) or {}).get("check_runs") or []
        by_name: dict[str, set] = {}
        for r in runs:
            state = "green" if r.get("conclusion") in ("success", "neutral", "skipped") else \
                "pending" if r.get("status") != "completed" else "red"
            by_name.setdefault(r.get("name") or "?", set()).add(state)
        status = (self._api("GET", f"commits/{sha}/status", check=False) or {})
        if status.get("total_count") and status.get("state") in ("pending", "failure", "error"):
            by_name["status"] = {"pending" if status["state"] == "pending" else "red"}
        if not by_name:
            return "pending"
        states = ["green" if "green" in s else "pending" if "pending" in s else "red" for s in by_name.values()]
        return "red" if "red" in states else "pending" if "pending" in states else "green"


class LocalHost:
    """No ticket system: the issue is a Markdown file, comments go to the run folder."""

    remote = False
    can_merge = False
    owner = "local"

    def __init__(self, issue_file: Path, ticket: int, run_dir: Path):
        self.ticket, self.run_dir = ticket, Path(run_dir)
        text = Path(issue_file).read_text()
        first, _, rest = text.partition("\n")
        self._issue = {"number": ticket, "title": first.lstrip("# ").strip(), "body": rest.strip(), "labels": []}

    def _human(self) -> Path:
        return self.run_dir / "human-comments.jsonl"

    def answer(self, body: str) -> None:
        with open(self._human(), "a") as fh:
            fh.write(json.dumps({"body": body, "created_at": events.now(), "by_owner": True}) + "\n")

    def default_branch(self) -> str:
        return "main"

    def issue(self) -> dict:
        comments = []
        if self._human().exists():
            comments = [json.loads(line) for line in self._human().read_text().splitlines() if line.strip()]
        return {**self._issue, "comments": comments}

    def comment(self, body: str) -> str:
        with open(self.run_dir / "comments.md", "a") as fh:
            fh.write(f"\n---\n{events.now()}\n\n{body}\n")
        print(f"[factory] Kommentar am Ticket:\n{body}\n", flush=True)
        return events.now()

    def labels(self, add: list[str] = (), remove: list[str] = ()) -> None:
        pass

    def labels_on(self, number: int, add: list[str] = (), remove: list[str] = ()) -> None:
        pass

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        """A local run has no ticket system: the parts are written down, numbered after this ticket."""
        path = self.run_dir / "created-issues.jsonl"
        number = self.ticket * 100 + 1 + (len(path.read_text().splitlines()) if path.exists() else 0)
        with open(path, "a") as fh:
            fh.write(json.dumps({"number": number, "title": title, "body": body, "labels": list(labels)}) + "\n")
        return number

    def issue_info(self, number: int) -> dict:
        return {"number": number, "state": "open", "body": "", "labels": []}

    def close_issue_number(self, number: int) -> None:
        pass

    def push(self, worktree: Path, branch: str) -> None:
        pass

    def open_pr(self, branch: str, base: str, title: str, body: str) -> str:
        (self.run_dir / "pr.md").write_text(f"# {title}\n\n{body}\n")
        return f"lokaler Branch {branch} (Beschreibung: {self.run_dir / 'pr.md'})"

    def close_issue(self) -> None:
        pass

    def dispatch(self, issue: int | None = None, dry_run: bool = False) -> None:
        with open(self.run_dir / "dispatched.jsonl", "a") as fh:
            fh.write(json.dumps({"issue": issue or self.ticket, **({"dry_run": True} if dry_run else {})}) + "\n")

    def open_issues(self, label: str) -> list[dict]:
        return []

    def active_runs(self) -> list[dict]:
        return []

    def branch_head(self, branch: str) -> str | None:
        return None

    def pull_request(self, url: str) -> dict | None:
        return None   # a local run has a branch, not a pull request

    def approve_waiting_runs(self, branch: str, sha: str, wait_s: int = 45) -> list[str]:
        return []

    def failed_log(self, run_id: str, limit: int = 12000, wait_s: int = 300) -> str:
        return ""

    def run_url(self, run_id: str) -> str:
        return f"run {run_id}"

    def checks_state(self, sha: str | None) -> str:
        return "green"

    def run_workflow(self, workflow: str, ref: str, inputs: dict | None = None) -> None:
        with open(self.run_dir / "workflows.jsonl", "a") as fh:
            fh.write(json.dumps({"workflow": workflow, "ref": ref, "inputs": inputs or {}}) + "\n")
