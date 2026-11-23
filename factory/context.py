"""What one run of the factory knows about its ticket."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import contracts, events


def new_status(ticket: int) -> dict:
    return {
        "schema": "status/1", "ticket": ticket, "phase": "intake", "round": 1,
        "branch": None, "base_branch": None, "base_sha": None, "waiting": None, "stopped": None,
        "spent_usd": 0.0, "budget_base": 0.0, "plan_rounds": 0, "rework_rounds": 0,
        "plan_approved": False, "plan_feedback": None, "done_tasks": [], "notes": [], "pr": None,
        "current": None, "run_urls": [], "workers": [],
    }


@dataclass
class Ctx:
    cfg: dict
    profile: dict
    repo: Path          # the product checkout
    workdir: Path       # scratch space: worktrees, state checkout, plugin cache
    ticket: int
    run_dir: Path       # this ticket's contract files
    status: dict
    host: Any = None    # ticket system and code host (GitHub or local files)
    store: Any = None   # where run_dir is persisted
    base_ref: str = "HEAD"
    base_branch: str | None = None
    log_dir: Path | None = None
    feedback: str | None = None
    published: float = 0.0   # when progress was last published
    # Workers of one ticket run at the same time and share the status, the event log and the state checkout.
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    seq: int = 0             # number of the last agent run, for its log and session file
    started: float = field(default_factory=time.time)   # when this run began: see pipeline.max_run_minutes

    def mode(self, station: str) -> str:
        return self.cfg["pipeline"]["modes"].get(station, "gate")

    def label(self, key: str) -> str:
        return self.cfg["human"]["labels"][key]

    def spend(self, usd: float) -> None:
        with self.lock:
            self.status["spent_usd"] = round(self.status.get("spent_usd", 0.0) + usd, 4)

    def next_seq(self, taken: int) -> int:
        """Number for the next agent run. `taken` is how many runs already left a log behind."""
        with self.lock:
            self.seq = max(self.seq, taken) + 1
            return self.seq

    def budget_left(self) -> float:
        limit = float(self.cfg["limits"]["budget_usd_per_ticket"])
        used = self.status.get("spent_usd", 0.0) - self.status.get("budget_base", 0.0)
        return max(0.0, round(limit - used, 4))

    def emit(self, station: str, kind: str, **fields) -> None:
        with self.lock:
            events.emit(self.run_dir, self.ticket, station, kind, round=self.status.get("round", 1), **fields)

    def note(self, text: str) -> None:
        with self.lock:
            if text not in self.status["notes"]:
                self.status["notes"].append(text)

    def checkpoint(self, station: str | None) -> None:
        """Publish where the ticket is right now, so the board can show the running station.
        Progress reporting must never stop the work, so a failed save is only logged."""
        with self.lock:
            self.status["current"] = station
            self.published = time.time()
            try:
                self.save(f"progress: {station or 'idle'}")
            except Exception as exc:
                print(f"[factory] could not publish progress: {exc}", flush=True)

    def publish(self, every: float = 15.0) -> None:
        """Publish progress while a station runs, but not more often than every few seconds."""
        with self.lock:
            if time.time() - self.published >= every:
                self.checkpoint(self.status.get("current"))

    def save(self, message: str) -> None:
        with self.lock:
            contracts.write_json(self.run_dir / "status.json", self.status, "status")
            if self.store is not None:
                self.store.save(f"#{self.ticket}: {message}")
