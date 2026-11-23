"""Configuration: factory.yaml holds the documented defaults, settings.yaml what the settings page changed."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent  # the factory repository

REQUIRED = ["roles", "pipeline", "verify", "limits", "runtime", "storage", "delivery", "human"]


def deep_merge(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _config_path(path: str | os.PathLike | None = None) -> Path:
    return Path(path or os.environ.get("FACTORY_CONFIG") or ROOT / "factory.yaml")


def load_defaults(path: str | os.PathLike | None = None) -> dict:
    """factory.yaml alone: the documented defaults."""
    p = _config_path(path)
    cfg = yaml.safe_load(p.read_text())
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise SystemExit(f"{p}: missing sections: {', '.join(missing)}")
    return cfg


def settings_path(path: str | os.PathLike | None = None) -> Path | None:
    """settings.yaml next to factory.yaml: what the settings page changed. FACTORY_SETTINGS=off ignores it."""
    env = os.environ.get("FACTORY_SETTINGS")
    if env == "off":
        return None
    return Path(env) if env else _config_path(path).parent / "settings.yaml"


# Settings only this machine needs: which projects its board shows and how it watches its own processes. They stay
# in settings.local.yaml, which git ignores, so a published factory does not carry one owner's project list.
LOCAL = {"board": ("repos", "local"), "machine": None}


def local_path(path: str | os.PathLike | None = None) -> Path | None:
    p = settings_path(path)
    return None if p is None else p.with_name(p.stem + ".local" + p.suffix)


def split_local(overlay: dict) -> tuple[dict, dict]:
    """(shared, local): the settings every run reads, and the ones that belong to this machine."""
    shared, local = {k: v for k, v in overlay.items()}, {}
    for key, fields in LOCAL.items():
        if not isinstance(shared.get(key), dict):
            continue
        if fields is None:
            local[key] = shared.pop(key)
            continue
        part = {f: shared[key][f] for f in fields if f in shared[key]}
        if part:
            local[key] = part
            rest = {f: v for f, v in shared[key].items() if f not in fields}
            if rest:
                shared[key] = rest
            else:
                shared.pop(key)
    return shared, local


def _read(p: Path | None) -> dict:
    if p is None or not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{p}: must be a mapping of settings")
    return data


def load_settings(path: str | os.PathLike | None = None) -> dict:
    return deep_merge(_read(settings_path(path)), _read(local_path(path)))


def problems(cfg: dict) -> list[str]:
    """Everything wrong with a merged configuration, one line each. Empty means it can run."""
    from jsonschema import Draft202012Validator

    from .contracts import schema
    errors = sorted(Draft202012Validator(schema("config")).iter_errors(cfg), key=lambda e: [str(x) for x in e.path])
    found = [f"{'.'.join(str(x) for x in e.path) or '(oberste Ebene)'}: {e.message}" for e in errors]
    if not found:   # cross-references only make sense on a well-formed configuration
        capabilities = cfg["runtime"]["capabilities"]
        for job, capability in cfg["runtime"]["jobs"].items():
            if capability not in capabilities:
                found.append(f"runtime.jobs.{job}: '{capability}' is not one of the capabilities "
                             f"({', '.join(capabilities)})")
        for name, role in cfg["roles"].items():
            for key in ("prompt", "rework_prompt"):
                if role.get(key) and not (ROOT / role[key]).is_file():
                    found.append(f"roles.{name}.{key}: file {role[key]} does not exist in the factory repository")
        from .harness import problems as harness_problems
        found += harness_problems(cfg)
        found += prompt_problems(cfg)
        pipeline = cfg["pipeline"]
        if pipeline["escalate_after"] > pipeline["max_rework_rounds"] > 0:
            found.append("pipeline.escalate_after: is larger than max_rework_rounds, so the escalation model would never run")
    return found


PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def prompt_files(cfg: dict) -> list[str]:
    """Every prompt file a station reads, plus the part all stations share."""
    files = [role[key] for role in cfg["roles"].values() for key in ("prompt", "rework_prompt") if role.get(key)]
    return list(dict.fromkeys(files + ["stations/_common.md"]))


def prompt_problems(cfg: dict) -> list[str]:
    """A prompt changed on the settings page may only use the placeholders its file uses: the station fills those
    and no others, so an unknown one would stop the run."""
    found = []
    for path, text in (cfg.get("prompts") or {}).items():
        if not (ROOT / path).is_file():
            found.append(f"prompts.{path}: no prompt file of that name in the factory repository")
            continue
        unknown = sorted(set(PLACEHOLDER.findall(text or "")) - set(PLACEHOLDER.findall((ROOT / path).read_text())))
        if unknown:
            found.append(f"prompts.{path}: unknown placeholders {', '.join('{{' + u + '}}' for u in unknown)}; "
                         "only those of the original prompt are filled")
    return found


def check(cfg: dict, where: str) -> dict:
    found = problems(cfg)
    if found:
        raise SystemExit(f"{where}: the configuration cannot be used:\n" + "\n".join(f"- {line}" for line in found[:12]))
    return cfg


def load_config(path: str | os.PathLike | None = None) -> dict:
    """What the factory runs with before a project's overrides: factory.yaml, then settings.yaml on top."""
    return check(deep_merge(load_defaults(path), load_settings(path)), "factory.yaml + settings.yaml")


def workdir(repo: Path) -> Path:
    """Scratch space of the factory: worktrees, state checkout, plugin cache."""
    if os.environ.get("FACTORY_WORKDIR"):
        d = Path(os.environ["FACTORY_WORKDIR"])
    elif os.environ.get("RUNNER_TEMP"):
        d = Path(os.environ["RUNNER_TEMP"]) / "factory"
    else:
        d = Path.home() / ".cache" / "software-factory" / repo.name
    d.mkdir(parents=True, exist_ok=True)
    return d


def sandboxed() -> bool:
    """True on throwaway runners, where an agent may run without permission prompts."""
    return os.environ.get("FACTORY_SANDBOXED") == "1"


def fake_agent() -> bool:
    """Dry run: canned station outputs instead of model calls. Tests the plumbing."""
    return os.environ.get("FACTORY_FAKE_AGENT") == "1"
