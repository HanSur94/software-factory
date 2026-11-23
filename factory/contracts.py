"""Contract files: the only way subsystems talk to each other."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .config import ROOT


class ContractError(ValueError):
    pass


_schemas: dict[str, dict] = {}


def schema(name: str) -> dict:
    if name not in _schemas:
        _schemas[name] = json.loads((ROOT / "contracts" / f"{name}.schema.json").read_text())
    return _schemas[name]


def validate(obj, name: str):
    errors = sorted(Draft202012Validator(schema(name)).iter_errors(obj), key=lambda e: [str(x) for x in e.path])
    if errors:
        lines = [f"- {'/'.join(str(x) for x in e.path) or '(top level)'}: {e.message}" for e in errors[:8]]
        raise ContractError(f"does not fit contract {name}/1:\n" + "\n".join(lines))
    return obj


def read_json(path: Path, name: str) -> dict:
    path = Path(path)
    if not path.exists():
        raise ContractError(f"{path.name} was not written")
    try:
        obj = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path.name} is not valid JSON: {exc}") from exc
    return validate(obj, name)


def write_json(path: Path, obj: dict, name: str) -> None:
    validate(obj, name)
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)\Z", re.S)


def read_frontmatter(path: Path, name: str) -> tuple[dict, str]:
    """Markdown contract: YAML front matter (validated) plus free text for humans."""
    path = Path(path)
    if not path.exists():
        raise ContractError(f"{path.name} was not written")
    match = _FRONT_MATTER.match(path.read_text())
    if not match:
        raise ContractError(f"{path.name} must start with a YAML block between two '---' lines")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ContractError(f"{path.name}: front matter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise ContractError(f"{path.name}: front matter must be a mapping")
    return validate(meta, name), match.group(2)


def read_text(path: Path, _name: str = "") -> str:
    path = Path(path)
    if not path.exists() or not path.read_text().strip():
        raise ContractError(f"{path.name} was not written or is empty")
    return path.read_text()
