"""RTK (Rust Token Killer): shorter shell output for the agents.

rtk rewrites a shell command into its own compact form (`cat a.py` -> `rtk read a.py`,
`python3 -m pytest` -> `rtk pytest`): the same program runs, but what comes back to the
model is filtered, so a test run or a grep costs a fraction of the tokens. It hooks into
Claude Code before every Bash call (factory/rtk_hook.py).

Switched on with agent.rtk; it acts only where rtk is installed on the runner, else the
agents run as before. Without a sandbox an agent may start only listed commands, and
Claude Code checks the rewritten command against that list: so the rtk form of every
listed command is listed as well, and the hook leaves a command alone whose rewrite
would not be allowed.
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).with_name("rtk_hook.py")

# The rtk forms of the reading commands Claude Code runs without asking (cat, head, tail, grep, ls, wc).
# They are on no list of the factory, so without these every rewrite into them was refused and RTK saved
# nothing on an unsandboxed runner: there the agents' own commands are mostly reads. Each one only reads
# and filters; rtk find, rg and tree are left out, since find -delete, rg --pre and tree -o reach further.
READ_ONLY = ["rtk read", "rtk grep", "rtk ls", "rtk wc"]
MARK = "__factory_rtk__"


def find(cfg: dict) -> str:
    """The rtk program when agent.rtk is on and rtk is installed here, else ""."""
    if not (cfg.get("agent") or {}).get("rtk"):
        return ""
    return shutil.which("rtk") or ""


def rewrite(rtk: str, command: str) -> str:
    """How rtk would rewrite a command, or "" when it leaves it alone."""
    try:
        out = subprocess.run([rtk, "hook", "check", command], text=True, capture_output=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    line = (out.strip().splitlines() or [""])[0].strip()
    return line if line.startswith("rtk ") else ""


def prefixes(rtk: str, allowed: list[str]) -> list[str]:
    """The rtk form of each allowed command prefix: `cat` -> `rtk read`, `python3 -m pytest` -> `rtk pytest`."""
    out: list[str] = []
    for prefix in allowed:
        for probe in (f"{prefix} {MARK}", prefix):
            got = rewrite(rtk, probe)
            if probe.endswith(MARK) and got.endswith(" " + MARK):
                got = got[:-len(MARK) - 1].strip()
            elif probe.endswith(MARK):
                continue
            if got and got not in out:
                out.append(got)
    return out


def allowed(rtk: str, prefixes_of_run: list[str]) -> list[str]:
    """The rtk command prefixes an unsandboxed run may start: the rtk form of each listed command, and the reads."""
    got = prefixes(rtk, prefixes_of_run)
    return got + [p for p in READ_ONLY if p not in got]


def hook_command() -> str:
    return f"{shlex.quote(sys.executable)} -I {shlex.quote(str(HOOK))}"


def gain(rtk: str, cwd: Path | str) -> dict:
    """rtk's running total for this folder and below: commands it shortened and tokens it saved."""
    try:
        out = subprocess.run([rtk, "gain", "-p", "-f", "json"], cwd=str(cwd), text=True, capture_output=True, timeout=15)
        summary = json.loads(out.stdout).get("summary") or {}
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return {}
    return {"commands": int(summary.get("total_commands") or 0), "saved": int(summary.get("total_saved") or 0),
            "input": int(summary.get("total_input") or 0)}


def since(before: dict, after: dict) -> dict:
    """What one run added to rtk's totals. Runs in folders below this one count too, so it is an estimate."""
    if not before or not after:
        return {}
    got = {k: max(0, after.get(k, 0) - before.get(k, 0)) for k in ("commands", "saved", "input")}
    return got if got["commands"] else {}
