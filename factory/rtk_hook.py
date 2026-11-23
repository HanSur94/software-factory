"""Claude Code PreToolUse hook of the factory, before every Bash call of an agent.

rtk's rewrite of the command, but only where it stays allowed: FACTORY_RTK names the rtk program,
FACTORY_RTK_ALLOW the command prefixes the run may start in their rtk form (a JSON list), or "*" on a
sandboxed runner where every command may run.

Runs as its own script (python3 -I factory/rtk_hook.py), so it imports nothing of the factory.
Whatever goes wrong, it prints nothing: the command then runs as the agent wrote it.
"""
import json
import os
import re
import subprocess
import sys

SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


def allowed(command: str, prefixes) -> bool:
    """Every part rtk wrote must start with a listed prefix; parts it left alone Claude Code checks as before."""
    if prefixes == "*":
        return True
    for part in SPLIT.split(command.strip()):
        if (part == "rtk" or part.startswith("rtk ")) and not any(part == p or part.startswith(p + " ") for p in prefixes):
            return False
    return True


def main(stdin: str, env: dict) -> str:
    rtk, allow = env.get("FACTORY_RTK"), env.get("FACTORY_RTK_ALLOW")
    if not rtk or not allow:
        return ""
    try:
        prefixes = "*" if allow == "*" else json.loads(allow)
        out = subprocess.run([rtk, "hook", "claude"], input=stdin, text=True, capture_output=True, timeout=10).stdout
        reply = json.loads(out)
        spec = reply["hookSpecificOutput"]
        command = spec["updatedInput"]["command"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return ""
    if not isinstance(command, str) or not allowed(command, prefixes):
        return ""
    # The allowlist decides, never the hook: a rewrite that says "allow" would let any command through.
    spec.pop("permissionDecision", None)
    return json.dumps(reply)


if __name__ == "__main__":
    sys.stdout.write(main(sys.stdin.read(), os.environ))
