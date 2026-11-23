"""RTK shortens shell output, but never lets an agent run more than it could without it."""

import json
import os
import stat
import subprocess
import sys

from factory import rtk, rtk_hook
from factory.harness import Request, get

# A stand-in for rtk: rewrites cat and pytest, counts what it shortened in a file next to it.
FAKE = r"""#!/bin/sh
dir=$(dirname "$0")
case "$1 $2" in
  "hook check")
    case "$3" in
      "cat "*) echo "rtk read ${3#cat }" ;;
      "python3 -m pytest"*) echo "rtk pytest${3#python3 -m pytest}" ;;
      "rm "*) echo "rtk err $3" ;;
      *) echo "No rewrite for: $3" ;;
    esac ;;
  "hook claude")
    cmd=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["tool_input"]["command"])')
    new=$("$0" hook check "$cmd")
    case "$new" in rtk*) printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","updatedInput":{"command":"%s"}}}' "$new" ;; esac ;;
  "gain -p")
    n=$(cat "$dir/count" 2>/dev/null || echo 0)
    printf '{"summary":{"total_commands":%s,"total_saved":%s,"total_input":%s}}' "$n" "$((n * 900))" "$((n * 1000))" ;;
esac
"""


def fake_rtk(tmp_path):
    path = tmp_path / "bin" / "rtk"
    path.parent.mkdir()
    path.write_text(FAKE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def hook(stdin_command, env):
    return rtk_hook.main(json.dumps({"tool_name": "Bash", "tool_input": {"command": stdin_command}}), env)


def test_the_rtk_form_of_every_allowed_command_is_allowed_too(tmp_path):
    exe = fake_rtk(tmp_path)
    assert rtk.prefixes(exe, ["cat", "python3 -m pytest", "git diff"]) == ["rtk read", "rtk pytest"]


def test_the_hook_rewrites_only_into_allowed_commands_and_never_says_allow(tmp_path):
    exe = fake_rtk(tmp_path)
    env = {"FACTORY_RTK": exe, "FACTORY_RTK_ALLOW": json.dumps(["rtk read", "rtk pytest"])}
    out = json.loads(hook("cat a.py", env))["hookSpecificOutput"]
    assert out["updatedInput"]["command"] == "rtk read a.py" and "permissionDecision" not in out
    assert hook("rm -rf x", env) == ""                   # `rtk err rm` would slip past the list: left alone
    assert hook("ls", env) == ""                         # nothing to rewrite
    assert hook("cat a.py", {**env, "FACTORY_RTK": str(tmp_path / "missing")}) == ""
    assert hook("cat a.py", {}) == ""                    # switched off
    assert json.loads(hook("rm -rf x", {**env, "FACTORY_RTK_ALLOW": "*"}))   # a sandbox may run anything


def test_compound_commands_are_judged_part_by_part():
    allow = ["rtk read"]
    assert rtk_hook.allowed("cd src && rtk read a.py", allow)
    assert not rtk_hook.allowed("rtk read a.py && rtk err make", allow)
    assert not rtk_hook.allowed("rtk", allow)


def test_the_hook_runs_as_its_own_script(tmp_path):
    exe = fake_rtk(tmp_path)
    env = {**os.environ, "FACTORY_RTK": exe, "FACTORY_RTK_ALLOW": '["rtk read"]'}
    proc = subprocess.run([sys.executable, "-I", str(rtk.HOOK)], input='{"tool_input":{"command":"cat b"}}',
                          text=True, capture_output=True, env=env, timeout=30)
    assert json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]["command"] == "rtk read b"


def test_claude_gets_the_hook_and_the_rtk_forms_on_its_allowlist(tmp_path):
    req = Request(prompt="p", cwd=tmp_path, model="m", tools="run", agent_commands=["python3 -m pytest"],
                  rtk_hook="python3 hook.py", rtk_allowed=["rtk pytest"])
    claude = get("claude")
    cmd = claude.argv(req)
    hooks = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]["PreToolUse"][0]
    assert hooks["matcher"] == "Bash" and hooks["hooks"][0]["command"] == "python3 hook.py"
    assert "Bash(rtk pytest:*)" in cmd and "Bash(python3 -m pytest:*)" in cmd
    assert claude.shell_prefixes(req) == ["git diff", "git log", "git show", "git status", "python3 -m pytest"]
    assert "--settings" not in claude.argv(Request(prompt="p", cwd=tmp_path, model="m"))


def test_switched_off_or_not_installed_means_no_rtk(monkeypatch, tmp_path):
    assert rtk.find({"agent": {"rtk": False}}) == ""
    monkeypatch.setenv("PATH", str(tmp_path))
    assert rtk.find({"agent": {"rtk": True}}) == ""
    exe = fake_rtk(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert rtk.find({"agent": {"rtk": True}}) == exe


def test_a_run_reports_what_rtk_saved(tmp_path):
    exe = fake_rtk(tmp_path)
    before = rtk.gain(exe, tmp_path)
    (tmp_path / "bin" / "count").write_text("3")
    assert rtk.since(before, rtk.gain(exe, tmp_path)) == {"commands": 3, "saved": 2700, "input": 3000}
    assert rtk.since(before, before) == {} and rtk.since({}, before) == {}


# A claude stand-in that calls its PreToolUse hook the way Claude Code does, then reports what came back.
CLAUDE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
sys.stdin.read()
hook = json.loads(sys.argv[sys.argv.index("--settings") + 1])["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
out = subprocess.run(hook, shell=True, text=True, capture_output=True,
                     input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat a.py"}})).stdout
open(os.path.join(os.path.dirname(os.environ["FACTORY_RTK"]), "count"), "w").write("2")
allowed = sys.argv[sys.argv.index("--allowedTools") + 1:]
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                  "result": json.dumps({"hook": out, "allowed": "Bash(rtk read:*)" in allowed})}))
'''


def test_an_agent_run_on_an_unsandboxed_runner_goes_through_rtk(tmp_path, monkeypatch):
    from factory.agent import run_agent
    exe = fake_rtk(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("FACTORY_SANDBOXED", raising=False)
    stub = tmp_path / "claude-stub"
    stub.write_text(CLAUDE)
    stub.chmod(0o755)
    cfg = {"agent": {"command": str(stub), "rtk": True}, "roles": {"implementer": {"tools": "run"}}}
    result = run_agent(cfg, {"agent_commands": ["cat"]}, "implementer", "hi", tmp_path, model="m", effort=None,
                       budget_usd=1.0, dirs=[], plugin_dirs=[])
    said = json.loads(result.text)
    assert result.ok and said["allowed"] and exe
    assert json.loads(said["hook"])["hookSpecificOutput"]["updatedInput"]["command"] == "rtk read a.py"
    assert result.rtk == {"commands": 2, "saved": 1800, "input": 2000}


def test_reads_go_through_rtk_even_when_no_listed_command_has_an_rtk_form(tmp_path, monkeypatch):
    # The Mac runners: a reviewer may start only git commands, which rtk leaves alone. Its many cat and grep
    # calls Claude Code allows by itself; without the rtk forms of those reads, RTK never shortened a thing.
    from factory.agent import run_agent
    exe = fake_rtk(tmp_path)
    assert rtk.allowed(exe, ["git diff", "git log"]) == rtk.READ_ONLY
    assert rtk.allowed(exe, ["cat", "python3 -m pytest"]) == ["rtk read", "rtk pytest", "rtk grep", "rtk ls", "rtk wc"]
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("FACTORY_SANDBOXED", raising=False)
    stub = tmp_path / "claude-stub"
    stub.write_text(CLAUDE)
    stub.chmod(0o755)
    cfg = {"agent": {"command": str(stub), "rtk": True}, "roles": {"reviewer": {"tools": "read"}}}
    result = run_agent(cfg, {}, "reviewer", "hi", tmp_path, model="m", effort=None,
                       budget_usd=1.0, dirs=[], plugin_dirs=[])
    said = json.loads(result.text)
    assert result.ok and said["allowed"]
    assert json.loads(said["hook"])["hookSpecificOutput"]["updatedInput"]["command"] == "rtk read a.py"
