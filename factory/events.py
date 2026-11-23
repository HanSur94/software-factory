"""Measurement: every station writes events in one format."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(run_dir: Path, ticket: int, station: str, kind: str, **fields) -> dict:
    event = {"schema": "event/1", "ts": now(), "ticket": ticket, "station": station, "kind": kind}
    event.update({k: v for k, v in fields.items() if v is not None})
    with open(Path(run_dir) / "events.jsonl", "a") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    extras = " ".join(f"{k}={v}" for k, v in event.items() if k not in ("schema", "ts", "ticket", "station", "kind"))
    print(f"[factory] #{ticket} {station:<12} {kind:<8} {extras}", flush=True)
    return event


def read(run_dir: Path) -> list[dict]:
    path = Path(run_dir) / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
