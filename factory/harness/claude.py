"""Claude Code: `claude -p` with --output-format stream-json."""
from __future__ import annotations

import json
from pathlib import Path

from . import Final, Harness, Reader, Request

# Without a sandbox the file tools stay inside the working directories, and only
# these commands run without a human saying yes.
READ_ONLY_COMMANDS = ["git diff", "git log", "git show", "git status"]
ALWAYS_ALLOWED = ["Agent", "Task", "Skill", "TodoWrite"]
FILE_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")


def shell_commands(tools: str, agent_commands: list[str]) -> list[str]:
    return list(READ_ONLY_COMMANDS) + (list(agent_commands) if tools in ("write", "run") else [])


def allowed_tools(tools: str, agent_commands: list[str], extra: list[str] = ()) -> list[str]:
    return list(ALWAYS_ALLOWED) + [f"Bash({c}:*)" for c in [*shell_commands(tools, agent_commands), *extra]]


def rtk_settings(hook: str) -> str:
    """--settings for one run: RTK's hook before every Bash call (factory/rtk.py)."""
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": hook, "timeout": 20}]}]}})


def describe(name: str, args: dict, cwd: str) -> str:
    """One short line saying what a tool call did, for the activity view."""
    if name == "Bash":
        text = (args.get("command") or "").strip().splitlines()[0] if (args.get("command") or "").strip() else ""
    elif name in FILE_TOOLS:
        path = str(args.get("file_path") or args.get("notebook_path") or "")
        text = path[len(cwd) + 1:] if path.startswith(cwd + "/") else path.rsplit("/", 1)[-1]
    elif name in ("Grep", "Glob"):
        text = str(args.get("pattern") or "")
    elif name == "Skill":
        text = str(args.get("skill") or "")
    elif name in ("Agent", "Task"):
        text = str(args.get("description") or "")
    else:
        text = ""
    return text[:160]


class Stream(Reader):
    """Reads --output-format stream-json: the final result message, and on the way
    every tool the agent called."""

    def __init__(self, cwd: Path | str = ""):
        super().__init__(cwd)
        self.result: dict = {}

    def feed(self, line: str) -> list[dict]:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                self.noise = (self.noise + [line.strip()])[-20:]
            return []
        if not isinstance(message, dict):
            return []
        if message.get("type") == "result":
            self.result = message
            return []
        if message.get("type") == "rate_limit_event":
            # With a Claude subscription the CLI reports how much of each limit window is used.
            for window, info in ((message.get("rate_limit_info") or {}).get("unifiedWindows") or {}).items():
                if isinstance(info, dict) and info.get("utilization") is not None:
                    seen = self.subscription.setdefault(window, {"start": info["utilization"]})
                    seen.update(end=info["utilization"], resets_at=info.get("resetsAt"))
            return []
        # Usually the model message; some lines (a retry notice, an error) carry a plain string here.
        body = message.get("message") if isinstance(message.get("message"), dict) else {}
        usage = body.get("usage")
        if message.get("type") == "assistant" and isinstance(usage, dict) and not message.get("parent_tool_use_id"):
            # One model call of the main agent: everything it read plus what it wrote is what sat in its context.
            self.context_peak = max(self.context_peak, sum(int(usage.get(k) or 0) for k in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")))
        content = body.get("content")
        if not isinstance(content, list):
            return []
        happened = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if message.get("type") == "assistant" and kind == "text" and str(block.get("text") or "").strip():
                happened.append({"type": "text", "text": block["text"][:4000]})
            elif message.get("type") == "assistant" and kind == "tool_use":
                name, args = str(block.get("name") or "?"), block.get("input") or {}
                self.tools[name] = self.tools.get(name, 0) + 1
                if name == "Skill" and args.get("skill") and args["skill"] not in self.skills:
                    self.skills.append(args["skill"])
                elif name in ("Agent", "Task"):
                    self.subagents += 1
                happened.append({"type": "tool", "tool": name, "text": describe(name, args, self.cwd),
                                 "input": json.dumps(args, ensure_ascii=False)[:1500]})
            elif message.get("type") == "user" and kind == "tool_result":
                body = block.get("content")
                if isinstance(body, list):
                    body = "\n".join(str(part.get("text") or "") for part in body
                                     if isinstance(part, dict) and part.get("type") == "text")
                happened.append({"type": "result", "text": str(body or "")[:1500], "error": bool(block.get("is_error"))})
        return happened

    def final(self, model: str) -> Final:
        data = self.result
        usage, models = data.get("usage") or {}, data.get("modelUsage") or {}
        window = (models.get(model) or max(models.values(), key=lambda m: m.get("costUSD") or 0, default={})).get("contextWindow")
        return Final(
            done=bool(data), error=bool(data.get("is_error")), kind=str(data.get("subtype") or ""),
            text=str(data.get("result") or ""), cost_usd=float(data.get("total_cost_usd") or 0.0),
            turns=int(data.get("num_turns") or 0), context_window=int(window or 0),
            tokens={"input": int(usage.get("input_tokens") or 0), "output": int(usage.get("output_tokens") or 0),
                    "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                    "cache_write": int(usage.get("cache_creation_input_tokens") or 0)} if usage else {})


def read_stream(stdout: str) -> tuple[dict, list[str], int, int]:
    stream = Stream()
    for line in stdout.splitlines():
        stream.feed(line)
    return stream.result, stream.skills, sum(stream.tools.values()), stream.subagents


class Claude(Harness):
    name = "claude"
    label = "Claude Code"
    command = "claude"
    package = "@anthropic-ai/claude-code"
    capabilities = frozenset({"budget_cap", "fallback_model", "effort", "tool_allowlist", "plugins", "skill_tool",
                              "slash_commands", "cost", "subscription", "shell_hook"})
    auth_env = frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"})
    env_keep = frozenset({"CLAUDE_CONFIG_DIR"})
    secrets = {"CLAUDE_CODE_OAUTH_TOKEN": "Zugang der Agents zu Claude (aus `claude setup-token`)"}
    models = ["claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]

    def argv(self, req: Request) -> list[str]:
        s = req.settings
        cmd = [req.command or self.command, "-p", "--output-format", "stream-json", "--verbose",
               "--no-session-persistence", "--model", req.model, "--max-budget-usd", f"{req.budget_usd:.2f}"]
        if req.effort:
            cmd += ["--effort", req.effort]
        if req.fallback and req.fallback != req.model:
            cmd += ["--fallback-model", req.fallback]
        if s.get("setting_sources"):
            cmd += ["--setting-sources", s["setting_sources"]]
        if s.get("strict_mcp", True):
            cmd += ["--strict-mcp-config"]
        if req.rtk_hook:
            cmd += ["--settings", rtk_settings(req.rtk_hook)]
        for d in req.dirs:
            cmd += ["--add-dir", str(d)]
        for d in req.plugin_dirs:
            cmd += ["--plugin-dir", str(d)]
        if req.sandboxed:
            cmd += ["--permission-mode", "bypassPermissions"]
        else:
            cmd += ["--permission-mode", "acceptEdits", "--allowedTools",
                    *allowed_tools(req.tools, req.agent_commands, req.rtk_allowed)]
        return cmd

    def shell_prefixes(self, req: Request) -> list[str]:
        return shell_commands(req.tools, req.agent_commands)

    def reader(self, cwd: Path | str = "") -> Stream:
        return Stream(cwd)

    def skill_instruction(self, chosen: bool = False) -> str:
        # "Use them where they apply" was not enough: recorded runs invoked none of the offered skills.
        # So each skill gets its own condition (roles.<role>.skill_when) instead of a general "where they apply".
        if chosen:
            return ("Invoke a skill that has a condition with the Skill tool only when this run reaches that "
                    "condition, and skip it otherwise: each skill loaded costs context. Invoke a skill without a "
                    "condition at the point in the work it is meant for, and follow it. The pipeline records which "
                    "skills a run actually invoked.\n")
        return ("These skills are how this station is meant to work. Invoke each one with the Skill tool at the point "
                "in the work it is meant for, and follow it. The pipeline records which skills a run actually invoked.\n")

    def entry(self, entry: str, args: str) -> str:
        return f"{entry} {args}"
