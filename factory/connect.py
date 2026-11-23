"""Connect a GitHub repository to the factory from the settings page.

Connecting is what setup/attach-project.sh and a hand-made pull request did
before: the labels, the right for Actions to open pull requests, the three
files (profile, caller workflow, issue form) as a pull request the owner
merges, and the repository on the board. Nothing is pushed to a branch people
work on; secrets stay with the owner.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess

import yaml

from . import config, contracts, settings
from .config import ROOT, deep_merge
from .settings import REPO, SettingsError

BRANCH = "chore/attach-software-factory"            # not under factory/: that prefix belongs to tickets
BRANCH_DEFAULT = "chore/factory-workflow-on-default"
PROFILE, WORKFLOW, FORM = ".factory/profile.yaml", ".github/workflows/factory.yml", ".github/ISSUE_TEMPLATE/factory-ticket.md"
LABELS = [("factory:go", "0e8a16", "Startet die Factory für dieses Issue"),
          ("factory:running", "1d76db", "Die Factory arbeitet an diesem Issue"),
          ("factory:waiting", "fbca04", "Die Factory wartet auf eine Antwort"),
          ("factory:review", "5319e7", "Pull Request liegt zur Prüfung bereit"),
          ("factory:done", "0e8a16", "Von der Factory automatisch gemergt"),
          ("factory:needs-human", "d93f0b", "Die Factory kommt allein nicht weiter"),
          ("factory:rejected", "cccccc", "Kein Ticket für die Factory"),
          ("factory:queued", "c5def5", "Wartet auf einen freien Platz der Factory")]
CHECK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")

QUERY = """
query($owner: String!, $name: String!, $profile: String!, $workflow: String!, $root: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner isPrivate viewerPermission description
    defaultBranchRef { name }
    refs(refPrefix: "refs/heads/", first: 100) { nodes { name } }
    labels(first: 100) { nodes { name } }
    profile: object(expression: $profile) { ... on Blob { text } }
    workflow: object(expression: $workflow) { ... on Blob { text } }
    root: object(expression: $root) { ... on Tree { entries { name } } }
    pullRequests(states: OPEN, first: 50) { nodes { url title headRefName baseRefName } }
  }
}
"""


def _gh(*args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(["gh", *args], text=True, capture_output=True, input=stdin, timeout=90)
    if proc.returncode != 0:
        raise SettingsError((proc.stderr or proc.stdout).strip()[-300:] or "gh ist fehlgeschlagen")
    return proc.stdout


def _api(method: str, path: str, body: dict | None = None) -> dict:
    args = ["api", "-X", method, path] + (["--input", "-"] if body is not None else [])
    out = _gh(*args, stdin=json.dumps(body) if body is not None else None)
    return json.loads(out) if out.strip() else {}


def my_repos() -> list[str]:
    """Repositories the person can pick from, most recently pushed first."""
    out = _gh("api", "graphql", "-f", "query={viewer{repositories(first:100,ownerAffiliations:OWNER,"
              "orderBy:{field:PUSHED_AT,direction:DESC}){nodes{nameWithOwner isArchived}}}}")
    nodes = json.loads(out)["data"]["viewer"]["repositories"]["nodes"]
    return [n["nameWithOwner"] for n in nodes if not n["isArchived"]]


def suggest(files: set[str]) -> dict:
    """A first guess at setup and checks from what lies in the repository's root. The person corrects it."""
    if {"Assets", "ProjectSettings"} <= files:
        return {"kind": "Unity-Projekt", "capability": "self-hosted", "setup": "", "checks": "",
                "hint": "Unity braucht einen eigenen Runner mit Lizenz. Trage das Kommando ein, das deine Tests ohne Fenster startet."}
    if "package.json" in files:
        return {"kind": "Node-Projekt", "capability": "linux", "setup": "npm ci", "checks": "tests: npm test", "hint": ""}
    if files & {"pyproject.toml", "requirements.txt", "setup.py"}:
        setup = "python3 -m pip install -q -r requirements.txt" if "requirements.txt" in files else ""
        return {"kind": "Python-Projekt", "capability": "linux", "setup": setup, "checks": "tests: python3 -m pytest -q", "hint": ""}
    if "Cargo.toml" in files:
        return {"kind": "Rust-Projekt", "capability": "linux", "setup": "", "checks": "tests: cargo test", "hint": ""}
    if "go.mod" in files:
        return {"kind": "Go-Projekt", "capability": "linux", "setup": "", "checks": "tests: go test ./...", "hint": ""}
    if "Makefile" in files:
        return {"kind": "Projekt mit Makefile", "capability": "linux", "setup": "", "checks": "tests: make test", "hint": ""}
    return {"kind": "", "capability": "linux", "setup": "", "checks": "",
            "hint": "Die Factory hat nicht erkannt, wie die Tests laufen. Trage mindestens ein Kommando ein."}


def inspect(repo: str, base: str | None = None, board_repos: list[str] | None = None) -> dict:
    """Everything the page needs to show before and after connecting: what is there, what is missing."""
    if not REPO.match(repo or ""):
        raise SettingsError("Bitte als owner/name angeben, zum Beispiel HanSur94/mein-projekt.")
    owner, name = repo.split("/", 1)

    def ask(at: str) -> dict:
        out = _gh("api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"name={name}",
                  "-F", f"profile={at}:{PROFILE}", "-F", f"workflow=HEAD:{WORKFLOW}", "-F", f"root={at}:")
        return json.loads(out)["data"]["repository"]

    data = ask(base or "HEAD")
    if not data:
        raise SettingsError(f"{repo} gibt es nicht, oder dein GitHub-Zugang sieht es nicht.")
    if not data["defaultBranchRef"]:
        raise SettingsError(f"{repo} ist leer. Lege zuerst einen ersten Commit an.")
    default = data["defaultBranchRef"]["name"]
    # Pull requests that attach the factory: made here, or by hand with the same title.
    open_prs = [p for p in data["pullRequests"]["nodes"]
                if p["headRefName"] in (BRANCH, BRANCH_DEFAULT) or p["title"].startswith("chore(factory):")]
    if base is None:
        # A project that works on another branch than the default one says so in its workflow; before that
        # is merged, the pull request that carries the profile shows where it is going.
        named = re.search(r"^\s+base:\s*([\w./-]+)\s*$", (data["workflow"] or {}).get("text") or "", re.M)
        guess = named.group(1) if named else next((p["baseRefName"] for p in open_prs if p["baseRefName"] != default), None)
        if guess and guess != default:
            base, data = guess, ask(guess)
    base = base or default
    have = {n["name"] for n in data["labels"]["nodes"]}
    try:   # REST only; a used-up quota must not hide the rest of the picture
        secrets = {s["name"] for s in json.loads(_gh("secret", "list", "--repo", repo, "--json", "name"))}
        may_open = _api("GET", f"repos/{repo}/actions/permissions/workflow").get("can_approve_pull_request_reviews")
    except (SettingsError, ValueError):
        secrets, may_open = None, None
    model = None if secrets is None else bool(secrets & {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"})
    steps = [
        {"id": "labels", "title": "Labels der Factory", "ok": all(n in have for n, _, _ in LABELS)},
        {"id": "permission", "title": "Actions dürfen Pull Requests öffnen", "ok": may_open},
        {"id": "profile", "title": f"Profil auf {base}", "ok": data["profile"] is not None},
        {"id": "workflow", "title": f"Workflow auf {default}", "ok": data["workflow"] is not None},
        {"id": "board", "title": "Im Board", "ok": repo in (board_repos or [])},
        {"id": "secret", "title": "Zugang zu Claude als Secret", "ok": model, "manual": True,
         "command": f"gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo {repo}"},
    ]
    for item, target in ((steps[2], base), (steps[3], default)):   # a file that waits in an open pull request
        waiting = next((p["url"] for p in open_prs if p["baseRefName"] == target), None)
        if waiting and not item["ok"]:
            item["pull_request"] = waiting
    return {"repo": data["nameWithOwner"], "private": data["isPrivate"], "description": data["description"] or "",
            "can_admin": data["viewerPermission"] == "ADMIN", "default_branch": default, "base": base,
            "branches": sorted(n["name"] for n in data["refs"]["nodes"]),
            "suggestion": suggest({e["name"] for e in (data["root"] or {}).get("entries") or []}),
            "steps": steps, "pull_requests": [{"url": p["url"], "base": p["baseRefName"]} for p in open_prs],
            "connected": all(s["ok"] for s in steps if not s.get("manual"))}


def parse_checks(text: str) -> list[dict]:
    """One check per line: `name: command`, or only the command."""
    checks = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        head, sep, rest = line.partition(": ")
        if sep and CHECK_NAME.match(head) and rest.strip():
            name, run = head, rest.strip()
        else:
            name, run = f"check-{len(checks) + 1}", line
        checks.append({"name": name, "run": run, "timeout_minutes": 30})
    return checks


def build_profile(repo: str, form: dict, cfg: dict) -> str:
    """The profile as text, checked against the contract and against the configuration it will be merged into."""
    profile: dict = {"schema": "profile/1", "name": repo.split("/", 1)[1],
                     "description": (form.get("description") or "").strip() or f"Projekt {repo}"}
    if (form.get("setup") or "").strip():
        profile["setup"] = form["setup"].strip()
    profile["checks"] = parse_checks(form.get("checks") or "")
    if not profile["checks"]:
        raise SettingsError("Mindestens ein Check ist nötig: das Kommando, das die Tests des Projekts startet.")
    capability = form.get("capability") or cfg["runtime"]["jobs"]["work"]
    if capability not in cfg["runtime"]["capabilities"]:
        raise SettingsError(f"Den Runner {capability} kennt die Factory nicht.")
    if cfg["runtime"]["capabilities"][capability]["provider"] == "self-hosted":
        # Without a sandbox an agent may start only what is listed: the first word of every check.
        commands = sorted({c["run"].split()[0] for c in profile["checks"]} | set(form.get("agent_commands") or []))
        profile["agent_commands"] = [c for c in commands if re.match(r"^[A-Za-z0-9._/-]+$", c)]
    if capability != cfg["runtime"]["jobs"]["work"]:
        profile["runtime"] = {"work": capability, "judge": cfg["runtime"]["jobs"]["judge"]}
    if (form.get("scenarios_repo") or "").strip():
        profile["judge"] = {"scenarios_repo": form["scenarios_repo"].strip(), "pattern": "scenarios/**/*.md"}
    workflows = [w.strip() for w in form.get("trigger_workflows") or [] if w.strip()]
    if workflows:
        profile["overrides"] = {"delivery": {"trigger_workflows": workflows}}
    try:
        contracts.validate(profile, "profile")
    except contracts.ContractError as exc:
        raise SettingsError(f"Aus diesen Angaben entsteht kein gültiges Profil: {exc}") from exc
    found = config.problems(deep_merge(cfg, profile.get("overrides")))
    if found:
        raise SettingsError("Diese Angaben passen nicht: " + "; ".join(found[:4]))
    head = ("# Profil dieses Projekts für die Software Factory, angelegt über die Einstellungsseite.\n"
            "# Gelesen wird immer der Stand des Basis-Branches, nie der eines Ticket-Branches.\n")
    return head + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)


def build_workflow(base: str, default: str, ci: list[str] | None = None) -> str:
    """The caller workflow for the product repository. `ci` names the workflows (their `name:`) whose result on a
    factory branch sends a ticket back for rework; without them the trigger stays commented out."""
    text = (ROOT / "templates/product" / WORKFLOW).read_text()
    if base != default:   # tickets start from another branch than the default one
        text = text.replace("      # base: develop\n", f"      base: {base}\n")
        text = text.replace("    # with:\n    #   base: develop\n", f"    with:\n      base: {base}\n")
    if ci:
        names = ", ".join(json.dumps(n, ensure_ascii=False) for n in ci)
        text = text.replace("  # workflow_run:\n  #   workflows: [ci]\n  #   types: [completed]\n  #   branches: ['factory/**']\n",
                            f"  workflow_run:\n    workflows: [{names}]\n    types: [completed]\n    branches: ['factory/**']\n")
    return text


def workflow_names(repo: str, ref: str, files: list[str]) -> list[str]:
    """The `name:` of each of these workflow files; the file name where a file has none or cannot be read."""
    names = []
    for f in files:
        try:
            text = base64.b64decode(_api("GET", f"repos/{repo}/contents/.github/workflows/{f}?ref={ref}")["content"]).decode()
            names.append(str((yaml.safe_load(text) or {}).get("name") or f))
        except Exception:
            names.append(f)
    return names


def _commit(repo: str, onto: str, branch: str, files: dict[str, str], message: str) -> None:
    """One commit with these files on a fresh branch that starts at `onto`. Uses the API: no clone is needed."""
    tip = _api("GET", f"repos/{repo}/git/ref/heads/{onto}")["object"]["sha"]
    tree = _api("POST", f"repos/{repo}/git/trees", {
        "base_tree": _api("GET", f"repos/{repo}/git/commits/{tip}")["tree"]["sha"],
        "tree": [{"path": path, "mode": "100644", "type": "blob", "content": text} for path, text in files.items()]})
    commit = _api("POST", f"repos/{repo}/git/commits", {"message": message, "tree": tree["sha"], "parents": [tip]})
    try:
        _api("POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    except SettingsError:   # the branch is there from an earlier attempt: move it
        _api("PATCH", f"repos/{repo}/git/refs/heads/{branch}", {"sha": commit["sha"], "force": True})


def _pull_request(repo: str, branch: str, base: str, title: str, body: str, existing: list[dict]) -> str:
    for pr in existing:
        if pr["base"] == base:
            return pr["url"]
    return _api("POST", f"repos/{repo}/pulls", {"title": title, "head": branch, "base": base, "body": body})["html_url"]


def connect(repo: str, form: dict, board_repos: list[str]) -> dict:
    """Do every step that is still open. A step that fails is reported; the others still run."""
    base = (form.get("base") or "").strip() or None
    before = inspect(repo, base, board_repos)
    if not before["can_admin"]:
        raise SettingsError(f"Für {repo} fehlen dir die Rechte eines Besitzers (Labels, Einstellungen, Workflows).")
    base, default = before["base"], before["default_branch"]
    if base not in before["branches"]:
        raise SettingsError(f"Den Branch {base} gibt es in {repo} nicht.")
    cfg = config.load_config()
    todo = {s["id"] for s in before["steps"] if not s["ok"] and not s.get("pull_request")}   # a waiting pull request is not redone
    done: list[dict] = []

    def step(title: str, fn) -> None:
        try:
            done.append({"title": title, "ok": True, **(fn() or {})})
        except (SettingsError, KeyError, ValueError) as exc:
            done.append({"title": title, "ok": False, "detail": str(exc)[:300]})

    # The workflows started on a factory pull request are the CI whose red result sends a ticket back.
    started = [w.strip() for w in form.get("trigger_workflows") or [] if w.strip()]
    ci = workflow_names(repo, base, started) if started else []
    files = {}
    if "profile" in todo:   # built first: wrong input must stop before anything is changed
        files = {PROFILE: build_profile(repo, form, cfg), WORKFLOW: build_workflow(base, default, ci),
                 FORM: (ROOT / "templates/product" / FORM).read_text()}
    elif "workflow" in todo and base == default:
        files = {WORKFLOW: build_workflow(base, default, ci)}

    if "labels" in todo:
        def labels():
            for name, color, text in LABELS:
                _gh("label", "create", name, "--repo", repo, "--color", color, "--description", text, "--force")
        step("Labels angelegt", labels)
    if "permission" in todo:
        step("Actions dürfen Pull Requests öffnen",
             lambda: _api("PUT", f"repos/{repo}/actions/permissions/workflow",
                          {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": True}) and None)
    if files:
        def main_pr():
            _commit(repo, base, BRANCH, files, "chore(factory): attach the Software Factory")
            url = _pull_request(repo, BRANCH, base, "chore(factory): attach the Software Factory", (
                "Verbindet dieses Repo mit der Software Factory.\n\n"
                + "".join(f"- `{path}`\n" for path in files)
                + f"\nEin Issue mit dem Label `factory:go` wird danach geplant, umgesetzt und geprüft und endet als "
                  f"Pull Request nach `{base}`. Angelegt über die Einstellungsseite der Factory."), before["pull_requests"])
            return {"url": url, "detail": f"Pull Request nach {base}: bitte ansehen und mergen"}
        step("Profil, Workflow und Ticket-Vorlage als Pull Request", main_pr)
    if "workflow" in todo and base != default:
        def default_pr():
            _commit(repo, default, BRANCH_DEFAULT, {WORKFLOW: build_workflow(base, default, ci)},
                    "chore(factory): put the factory workflow on the default branch")
            url = _pull_request(repo, BRANCH_DEFAULT, default, "chore(factory): put the factory workflow on the default branch", (
                f"GitHub liest Issue- und Label-Ereignisse nur aus den Workflows des Standard-Branches. Deshalb muss "
                f"`{WORKFLOW}` auch auf `{default}` liegen. Die Datei ist dieselbe wie im Pull Request nach `{base}`; "
                f"den bitte zuerst mergen."), before["pull_requests"])
            return {"url": url, "detail": f"Pull Request nach {default}: nach dem ersten mergen"}
        step(f"Workflow auch auf {default}", default_pr)
    if "board" in todo:
        def board():
            saved = settings.save_global({"board": {"repos": [*cfg["board"].get("repos", []), repo]}})
            if not saved["ok"]:
                raise SettingsError("; ".join(saved["problems"]))
            board_repos.append(repo)   # the running board shows it without a restart
            try:
                return {"detail": settings.publish()["message"]}
            except SettingsError as exc:
                return {"detail": f"Lokal eingetragen. {exc}"}
        step("Ins Board aufgenommen", board)
    return {"done": done, "after": inspect(repo, base, board_repos)}


def remove_from_board(repo: str, board_repos: list[str]) -> dict:
    """Take a project off the board. Nothing in the repository changes; the factory still works there."""
    cfg = config.load_config()
    saved = settings.save_global({"board": {"repos": [r for r in cfg["board"].get("repos", []) if r != repo]}})
    if not saved["ok"]:
        raise SettingsError("; ".join(saved["problems"]))
    if repo in board_repos:
        board_repos.remove(repo)
    try:
        message = settings.publish()["message"]
    except SettingsError as exc:
        message = f"Lokal ausgetragen. {exc}"
    return {"message": f"{repo} ist nicht mehr im Board. {message}"}
