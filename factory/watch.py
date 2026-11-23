"""Tells the owner when a ticket needs them, without them watching the board.

Runs inside the board process (board.serve starts it as a thread). Every minute or two it
looks at the same cards the board shows and sends a macOS notification for what is new:

- a ticket stopped (with the reason),
- a ticket waits for the owner (a question, a plan to approve),
- a running ticket has had no event for board.stall_minutes (default 30),
- the disk of this machine has less free space than board.min_free_disk_gb.

Each alert is sent once; what was sent is remembered in ~/.cache/software-factory so a
restarted board does not repeat it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SEEN = Path.home() / ".cache" / "software-factory" / "board-alerts.json"


def _age_minutes(ts: str | None, now: float) -> float:
    if not ts:
        return 0.0
    then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    return (now - then) / 60


def free_disk_gb(path: str = "/") -> float:
    return shutil.disk_usage(path).free / 1e9


def alerts(cards: list[dict], *, now: float, stall_minutes: float, free_gb: float, min_free_gb: float) -> list[tuple]:
    """(key, title, text) for everything worth a notification right now. The key changes when the cause does."""
    found = []
    for c in cards:
        name = f"{c.get('repo', '').split('/')[-1]} #{c['number']}"
        title = (c.get("title") or "")[:80]
        last = (c.get("last") or {}).get("ts")
        if c["column"] == "stopped":
            found.append((f"{c['repo']}#{c['number']}:stopped:{last}", f"{name} angehalten", f"{title}: {c.get('detail', '')}"))
        elif c["column"] == "waiting":
            found.append((f"{c['repo']}#{c['number']}:waiting:{last}", f"{name} wartet auf dich",
                          f"{title}: {c.get('detail', '')}"))
        elif c.get("running") and c["column"] in ("queue", "plan", "build", "verify") \
                and _age_minutes(last, now) >= stall_minutes:
            found.append((f"{c['repo']}#{c['number']}:stalled:{last}", f"{name} hängt",
                          f"{title}: seit {int(_age_minutes(last, now))} Minuten kein Fortschritt "
                          f"({c.get('step', '')})"))
    if min_free_gb and free_gb < min_free_gb:
        found.append((f"disk:{int(free_gb)}", "Wenig Speicher",
                      f"Nur noch {free_gb:.1f} GB frei; unter dem Limit der Projekte warten Tickets."))
    return found


def notify(title: str, text: str) -> None:
    """A macOS notification. Elsewhere (or without osascript) the alert is only printed."""
    print(f"[factory] {title}: {text}", flush=True)
    script = f"display notification {json.dumps(text[:240])} with title {json.dumps('Software Factory')} " \
             f"subtitle {json.dumps(title[:120])} sound name \"Glass\""
    try:
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _load_seen() -> set:
    try:
        return set(json.loads(SEEN.read_text()))
    except (OSError, ValueError):
        return set()


def _store_seen(seen: set) -> None:
    try:
        SEEN.parent.mkdir(parents=True, exist_ok=True)
        SEEN.write_text(json.dumps(sorted(seen)[-500:]))
    except OSError:
        pass


def check_once(get_cards, conf: dict, seen: set, send=notify) -> list[tuple]:
    """One look: send what is new, remember it. Returns what was sent."""
    found = alerts(get_cards(), now=time.time(), stall_minutes=float(conf.get("stall_minutes", 30)),
                   free_gb=free_disk_gb(), min_free_gb=float(conf.get("min_free_disk_gb", 15)))
    sent = [a for a in found if a[0] not in seen]
    for key, title, text in sent:
        send(title, text)
        seen.add(key)
    return sent


def start(get_cards, conf: dict) -> None:
    """Watch in the background for as long as the board runs. board.notify: false turns it off."""
    if conf.get("notify") is False:
        return
    every = float(conf.get("watch_seconds", 90))

    def loop():
        # Without a memory of earlier alerts, what is already wrong when the board starts is not news.
        seen, first = _load_seen(), not SEEN.exists()
        while True:
            try:
                if first:   # what was already wrong when the board started is not news: remember it silently
                    check_once(get_cards, conf, seen, send=lambda title, text: None)
                    first = False
                else:
                    check_once(get_cards, conf, seen)
                _store_seen(seen)
            except Exception as exc:   # a failed look must not end the watch
                print(f"[factory] watch: {exc}", flush=True)
            time.sleep(every)

    threading.Thread(target=loop, name="factory-watch", daemon=True).start()
