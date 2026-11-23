"""How much of the Claude plan is used right now, read from Claude itself.

Agents report their plan usage only while they run, so between tickets those numbers age
and can show a window that has long been reset. The board asks Claude for the current
numbers instead, with the login of the Claude CLI on this machine. That is the same source
the CLI's own /usage shows. The login is only read, never refreshed: refreshing it here
would replace the token the CLI holds and sign it out.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

URL = "https://api.anthropic.com/api/oauth/usage"
CACHE_SECONDS = 180          # the endpoint answers 429 when asked too often
BACKOFF_SECONDS = 600        # after a 429 without Retry-After
# Top-level windows of the answer and the name the board gives them.
WINDOWS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "value": None, "good": None, "until": 0.0}


def _token() -> str | None:
    """The CLI's own login: the macOS keychain, else its credentials file. None when there is none."""
    raw = ""
    if sys.platform == "darwin":
        try:
            raw = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            raw = ""
    if not raw:
        path = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / ".credentials.json"
        raw = path.read_text() if path.is_file() else ""
    try:
        login = json.loads(raw).get("claudeAiOauth") or {} if raw else {}
    except ValueError:
        return None
    if not login.get("accessToken") or (login.get("expiresAt") or 0) / 1000 < time.time():
        return None   # an expired login is renewed by the CLI the next time it runs, not here
    return login["accessToken"]


def _epoch(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def windows(answer: dict, now: float) -> dict:
    """The answer of Claude's usage endpoint in the board's shape: window -> {used 0..1, resets_at, ts, live}."""
    ts = datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")
    out: dict = {}
    for name in WINDOWS:
        info = answer.get(name)
        if isinstance(info, dict) and info.get("utilization") is not None:
            out[name] = {"used": float(info["utilization"]) / 100, "resets_at": _epoch(info.get("resets_at")),
                         "ts": ts, "live": True, "models": []}
    # Limits that apply to one model only, e.g. a weekly limit for one model family.
    for limit in answer.get("limits") or []:
        model = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
        if limit.get("kind") != "weekly_scoped" or not model or limit.get("percent") is None:
            continue
        out[f"seven_day_{model.lower()}"] = {"used": float(limit["percent"]) / 100,
                                             "resets_at": _epoch(limit.get("resets_at")), "ts": ts, "live": True,
                                             "models": [], "model": model, "active": bool(limit.get("is_active"))}
    extra = answer.get("extra_usage") or {}
    if extra.get("is_enabled") and extra.get("monthly_limit"):
        out["extra_usage"] = {"used": float(extra.get("used_credits") or 0) / float(extra["monthly_limit"]),
                              "resets_at": None, "ts": ts, "live": True, "models": [],
                              "amount": f'{(extra.get("used_credits") or 0) / 100:.2f} / {extra["monthly_limit"] / 100:.2f} '
                                        f'{extra.get("currency") or ""}'.strip()}
    return out


def live(now: float | None = None, fetch=None) -> dict | None:
    """The current plan usage, asked at most every few minutes. None when it was never answered.

    When Claude does not answer, or asks to wait (429), the last answer stays: its "ts" says how old it is."""
    now = time.time() if now is None else now
    with _lock:
        if now - _cache["at"] < CACHE_SECONDS or now < _cache.get("until", 0.0):
            return _cache["value"]
        _cache["at"] = now
        try:
            answer = (fetch or _fetch)()
        except HTTPError as exc:
            if exc.code == 429:
                wait = exc.headers.get("Retry-After") if exc.headers else None
                _cache["until"] = now + (float(wait) if wait and str(wait).isdigit() else BACKOFF_SECONDS)
            answer = None
        except Exception:
            answer = None
        if answer:
            _cache["good"] = windows(answer, now)
        _cache["value"] = _cache.get("good")
        return _cache["value"]


def _fetch() -> dict | None:
    token = _token()
    if not token:
        return None
    request = Request(URL, headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
                                    "User-Agent": "software-factory-board"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def merge(reported: dict, current: dict | None, now: float) -> tuple[dict, str]:
    """Live numbers when Claude answered; else what agents reported, without windows that were reset since."""
    if current:
        return current, "live"
    fresh = {w: q for w, q in reported.items() if not q.get("resets_at") or q["resets_at"] > now}
    return fresh, "agents"
