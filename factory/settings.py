"""What the settings page reads and writes.

Three layers decide how the factory runs, each on top of the one before:
factory.yaml (documented defaults), settings.yaml (what the page changed for
every project) and the "overrides" block of a project's .factory/profile.yaml.
The page writes the second and the third; factory.yaml stays as it is.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess

import yaml

from . import config, contracts, harness
from .config import ROOT, deep_merge

PROFILE = ".factory/profile.yaml"
HEADER = ("# Von der Einstellungsseite geschrieben (python3 -m factory board, dann /settings).\n"
          "# Enthält nur, was von factory.yaml abweicht. Gilt für alle Projekte; ein Projekt\n"
          "# kann einzelne Werte in seinem Profil überschreiben.\n")
SECRETS = {"SCENARIOS_DEPLOY_KEY": "Lesezugriff des Richters auf das Szenarien-Repo"}


def models() -> list[str]:
    return [m for h in harness.all_harnesses().values() for m in h.models]


def secrets(cfg: dict) -> dict:
    """Repository secrets a project needs: the model access of every harness it runs in, then the rest."""
    out = {}
    for name in harness.used(cfg):
        out.update(harness.get(name).secrets)
    return {**out, **SECRETS}
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class SettingsError(ValueError):
    """Something the person at the page can fix; the message is shown to them."""


def diff(new: dict, base: dict) -> dict:
    """The part of `new` that differs from `base`: what has to be written on top of it."""
    out = {}
    for key, value in new.items():
        if key not in base:
            if value is not None:
                out[key] = value
        elif isinstance(value, dict) and isinstance(base[key], dict):
            inner = diff(value, base[key])
            if inner:
                out[key] = inner
        elif value != base[key]:
            out[key] = value
    return out


def _dump(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], text=True, capture_output=True, timeout=60)


def _file() -> str:
    path = config.settings_path()
    return path.name if path and path.parent == ROOT else ""


def git_state() -> dict:
    """Whether what is saved here has reached GitHub, where the runs read it."""
    name = _file()
    if not name or _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        return {"available": False}
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    ahead = _git("rev-list", "--count", "@{upstream}..HEAD")
    remote = _git("remote", "get-url", "origin").stdout.strip()
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", remote)
    return {"available": True, "branch": branch, "remote": match.group(1) if match else remote,
            "unsaved": bool(_git("status", "--porcelain", "--", name).stdout.strip()),
            "ahead": int(ahead.stdout.strip() or 0) if ahead.returncode == 0 else 0}


def with_prompts(cfg: dict) -> dict:
    """The defaults as the page shows them: every prompt file's text under `prompts`, so a field can show it and an
    edit that is the same as the file is no override."""
    files = {path: (ROOT / path).read_text() for path in config.prompt_files(cfg) if (ROOT / path).is_file()}
    return {**cfg, "prompts": {**files, **(cfg.get("prompts") or {})}}


def state(repos: list[str]) -> dict:
    defaults = with_prompts(config.load_defaults())
    path = config.settings_path()
    raw = path.read_text() if path and path.exists() else ""
    try:
        overlay, error = config.load_settings(), ""
    except (SystemExit, yaml.YAMLError) as exc:
        overlay, error = {}, f"settings.yaml ist nicht lesbar: {exc}"
    effective = deep_merge(defaults, overlay)
    return {"defaults": defaults, "effective": effective, "raw": raw, "error": error,
            "problems": config.problems(effective),
            "effective_yaml": _dump(deep_merge(config.load_defaults(), overlay)),   # without the long default prompts
            "git": git_state(), "repos": repos, "models": models(),
            "harnesses": [[h.name, h.label] for h in harness.all_harnesses().values()],
            "prompt_files": config.prompt_files(defaults)}


def save_global(values: dict | None = None, raw: str | None = None) -> dict:
    """Write settings.yaml: either the changed fields of the form, or the whole file as text."""
    path = config.settings_path()
    if path is None:
        raise SettingsError("FACTORY_SETTINGS=off: Auf diesem Rechner sind Einstellungen abgeschaltet.")
    defaults = with_prompts(config.load_defaults())
    if raw is not None:
        try:
            overlay = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise SettingsError(f"Kein gültiges YAML: {exc}") from exc
        if not isinstance(overlay, dict):
            raise SettingsError("settings.yaml muss eine Zuordnung von Einstellungen sein.")
        text = raw if raw.endswith("\n") or not raw else raw + "\n"
    else:
        if not isinstance(values, dict):
            raise SettingsError("Keine Werte übergeben.")
        overlay = diff(deep_merge(deep_merge(defaults, config.load_settings()), values), defaults)
        text = HEADER + (_dump(overlay) if overlay else "")
    found = config.problems(deep_merge(defaults, overlay))
    if found:
        return {"ok": False, "problems": found}
    path.write_text(text)
    return {"ok": True, "problems": []}


def publish() -> dict:
    """Commit settings.yaml alone and push it: GitHub runs read the factory from its main branch."""
    git, name = git_state(), _file()
    if not git.get("available"):
        raise SettingsError("Die Factory liegt hier nicht in einem Git-Checkout; veröffentlichen geht nur von dort.")
    if git["branch"] != "main":
        raise SettingsError(f"Der Checkout steht auf dem Branch {git['branch']}. Die Läufe auf GitHub lesen main; "
                            "wechsle dorthin und speichere noch einmal.")
    _git("add", "--", name)
    if _git("diff", "--cached", "--quiet", "--", name).returncode != 0:
        done = _git("commit", "-m", "Einstellungen über die Einstellungsseite geändert", "--", name)
        if done.returncode != 0:
            raise SettingsError("Commit fehlgeschlagen: " + (done.stderr or done.stdout).strip()[-300:])
    if _git("rev-list", "--count", "@{upstream}..HEAD").stdout.strip() in ("", "0"):
        return {"published": False, "message": "Auf GitHub steht schon derselbe Stand."}
    pushed = _git("push", "origin", "main")
    if pushed.returncode != 0:
        raise SettingsError("Lokal gespeichert und committet, aber der Push schlug fehl: "
                            + (pushed.stderr or pushed.stdout).strip()[-300:])
    return {"published": True, "message": f"Auf GitHub veröffentlicht ({git['remote']}, main)."}


# ---- one project: the "overrides" block of its profile -------------------------

def set_overrides(text: str, overrides: dict) -> str:
    """Replace the top-level `overrides:` block of a profile and leave every other line as it is."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"^overrides\s*:", line)), None)
    block = ["overrides:"] + ["  " + line for line in _dump(overrides).splitlines()] if overrides else []
    if start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ([""] + block) if block else []
    else:
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1   # blank lines after the block belong to what follows
        lines[start:end] = block
    return "\n".join(lines).rstrip("\n") + "\n"


def _gh(*args: str) -> str:
    proc = subprocess.run(["gh", *args], text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise SettingsError((proc.stderr or proc.stdout).strip()[-300:] or "gh ist fehlgeschlagen")
    return proc.stdout


WORKFLOW = ".github/workflows/factory.yml"


def base_branch(repo: str) -> str:
    """Where the factory reads the profile: `base:` in the caller workflow on the default branch, else the default."""
    default = json.loads(_gh("api", f"repos/{repo}"))["default_branch"]
    try:
        data = json.loads(_gh("api", f"repos/{repo}/contents/{WORKFLOW}"))
        named = re.search(r"^\s+base:\s*([\w./-]+)\s*$", base64.b64decode(data["content"]).decode(), re.M)
    except (SettingsError, ValueError, KeyError):
        named = None
    return named.group(1) if named else default


def _profile(repo: str, branch: str) -> tuple[str, str]:
    data = json.loads(_gh("api", f"repos/{repo}/contents/{PROFILE}?ref={branch}"))
    return base64.b64decode(data["content"]).decode(), data["sha"]


def read_project(repo: str) -> dict:
    if not REPO.match(repo):
        raise SettingsError("Unbekanntes Projekt.")
    branch = base_branch(repo)
    text, sha = _profile(repo, branch)
    profile = yaml.safe_load(text) or {}
    try:   # names and dates only; the values of secrets never leave GitHub
        present = {s["name"]: s.get("updatedAt", "") for s in json.loads(_gh("secret", "list", "--repo", repo, "--json", "name,updatedAt"))}
    except (SettingsError, ValueError):
        present = None
    try:   # the harnesses this project runs in decide which model access it needs
        needed = secrets(deep_merge(config.load_config(), profile.get("overrides")))
    except SystemExit:
        needed = secrets(config.load_defaults())
    return {"repo": repo, "sha": sha, "branch": branch, "overrides": profile.get("overrides") or {},
            "profile": {"name": profile.get("name", ""), "description": profile.get("description", ""),
                        "setup": profile.get("setup", ""), "checks": profile.get("checks") or [],
                        "runtime": profile.get("runtime") or {},
                        "scenarios_repo": (profile.get("judge") or {}).get("scenarios_repo", "")},
            "secrets": None if present is None else [
                {"name": name, "purpose": purpose, "set": name in present, "updated": present.get(name, "")}
                for name, purpose in needed.items()
                # without a scenarios repository the judge needs no key, so a missing one is not a gap
                if name != "SCENARIOS_DEPLOY_KEY" or (profile.get("judge") or {}).get("scenarios_repo")]}


def save_project(repo: str, overrides: dict, sha: str) -> dict:
    """Commit a project's overrides to its base branch, which is where the factory reads the profile.
    A branch protection that refuses the commit gets a pull request instead."""
    if not REPO.match(repo) or not isinstance(overrides, dict):
        raise SettingsError("Unbekanntes Projekt oder keine Werte.")
    branch = base_branch(repo)
    text, current = _profile(repo, branch)
    if sha != current:
        raise SettingsError("Das Profil wurde inzwischen geändert. Lade die Seite neu und trage die Änderung noch einmal ein.")
    merged = deep_merge(config.load_config(), overrides)
    found = config.problems(merged)
    if merged["pipeline"]["modes"].get("judge") == "gate" and not ((yaml.safe_load(text) or {}).get("judge") or {}).get("scenarios_repo"):
        # Without scenarios the judge gives no verdict, and a gate without a verdict stops every ticket before its PR.
        found.append("pipeline.modes.judge: Dieses Projekt hat kein Szenarien-Repo. Ohne Szenarien urteilt der Richter "
                     "nicht, und als Tor hielte er jedes Ticket vor dem Pull Request an. Lass ihn beraten.")
    if found:
        return {"ok": False, "problems": found}
    new = set_overrides(text, overrides)
    try:
        written = contracts.validate(yaml.safe_load(new), "profile")
    except (contracts.ContractError, yaml.YAMLError) as exc:
        raise SettingsError(f"Das Profil wäre danach nicht mehr gültig: {exc}") from exc
    if (written.get("overrides") or {}) != overrides:
        raise SettingsError("Der Abschnitt overrides ließ sich im Profil nicht sauber ersetzen; bitte von Hand ändern.")
    if new == text:
        return {"ok": True, "problems": [], "message": "Nichts geändert."}
    message = "Factory: Einstellungen des Projekts geändert"
    try:
        done = json.loads(_gh("api", "-X", "PUT", f"repos/{repo}/contents/{PROFILE}", "-f", f"message={message}",
                              "-f", f"content={base64.b64encode(new.encode()).decode()}", "-f", f"sha={sha}",
                              "-f", f"branch={branch}"))
    except SettingsError as exc:
        if "protect" not in str(exc).lower() and "status check" not in str(exc).lower():
            raise
        from . import connect   # the branch is protected: same change as a pull request
        connect._commit(repo, branch, "chore/factory-settings", {PROFILE: new}, f"chore(factory): {message}")
        url = connect._pull_request(repo, "chore/factory-settings", branch, f"chore(factory): {message}",
                                    "Geänderte Einstellungen der Factory für dieses Projekt (Abschnitt `overrides` im "
                                    "Profil). Angelegt über die Einstellungsseite, weil der Branch geschützt ist.", [])
        return {"ok": True, "problems": [], "commit": url,
                "message": f"{branch} ist geschützt: Die Änderung liegt als Pull Request bereit. Nach dem Merge gilt sie."}
    return {"ok": True, "problems": [], "message": f"In {repo} auf {branch} gespeichert; der nächste Lauf nutzt die neuen Werte.",
            "commit": (done.get("commit") or {}).get("html_url", "")}
