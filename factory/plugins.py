"""Skill packs: fetched once, pinned to a commit, loaded only by stations that name them."""
from __future__ import annotations

import subprocess
from pathlib import Path


def plugin_root(workdir: Path) -> Path:
    return workdir / "plugins"


def fetch(cfg: dict, workdir: Path) -> list[str]:
    done = []
    root = plugin_root(workdir)
    root.mkdir(parents=True, exist_ok=True)
    for name, spec in (cfg.get("plugins") or {}).items():
        if not spec.get("enabled"):
            continue
        dest = root / name
        if not (dest / ".git").exists():
            subprocess.run(["git", "clone", "--quiet", "--filter=blob:none",
                            f"https://github.com/{spec['repo']}.git", str(dest)], check=True)
        current = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, text=True, capture_output=True).stdout.strip()
        if current != spec["ref"]:
            subprocess.run(["git", "fetch", "--quiet", "origin", spec["ref"]], cwd=dest, check=False)
            subprocess.run(["git", "checkout", "--quiet", "--detach", spec["ref"]], cwd=dest, check=True)
        done.append(name)
    return done


def available(cfg: dict, workdir: Path, name: str) -> bool:
    spec = (cfg.get("plugins") or {}).get(name) or {}
    return bool(spec.get("enabled")) and (plugin_root(workdir) / name).is_dir()


def needed_by(rc: dict) -> list[str]:
    """Plugin names a role refers to, through 'plugin:skill' entries or a '/plugin:command' entry."""
    names = [s.split(":", 1)[0] for s in (rc.get("skills") or []) + (rc.get("sensitive_skills") or []) if ":" in s]
    entry = rc.get("entry") or ""
    if entry.startswith("/") and ":" in entry:
        names.append(entry[1:].split(":", 1)[0])
    return sorted(set(names))
