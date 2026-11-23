"""The executing agent: one fresh, headless run per station, in the harness the role names.

What a CLI is called, which flags it takes and what it prints lives in factory/harness.
This file is the part every harness shares: the scrubbed environment, the timeout, the
live view of the session, and the AgentResult a station gets back.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import harness as harnesses
from . import rtk as rtks
from .config import sandboxed

# Agents and project checks get a scrubbed environment: no GitHub token, nothing
# of the runner beyond what a build needs.
ENV_KEEP = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ",
    "CI", "RUNNER_OS", "RUNNER_TEMP", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "XDG_CONFIG_HOME",
}


def clean_env(cfg: dict, profile: dict, harness: harnesses.Harness | None = None) -> dict:
    """The environment of an agent run (with its harness, which adds the model credentials) or of a project check."""
    keep = set(ENV_KEEP) | set(cfg.get("agent", {}).get("env_passthrough") or [])
    keep |= set(profile.get("env_passthrough") or [])
    if harness:
        keep |= harness.auth_env | harness.env_keep
    env = {k: v for k, v in os.environ.items() if k in keep and v != ""}  # an unset secret arrives as ""
    env.update({k: str(v) for k, v in (profile.get("env") or {}).items()})
    return env


@dataclass
class AgentResult:
    ok: bool
    text: str = ""
    error: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    model: str = ""
    skills: list = field(default_factory=list)   # skills the agent actually invoked, in order
    turns: int = 0
    tool_calls: int = 0
    subagents: int = 0
    tools: dict = field(default_factory=dict)    # tool name -> number of calls
    tokens: dict = field(default_factory=dict)   # input, output, cache_read, cache_write
    context_peak: int = 0                        # most tokens in the context window at any point of the run
    context_window: int = 0                      # size of the model's context window
    subscription: dict = field(default_factory=dict)   # window -> {start, end, resets_at}: share of the plan's limit used
    rtk: dict = field(default_factory=dict)      # commands RTK shortened and tokens it saved (estimate), when it ran
    harness: str = ""


def redact(text: str) -> str:
    """Take credentials out of anything that is written down. An agent that prints its
    environment must not put the model token into the ticket's history."""
    for key in (*sorted(harnesses.all_auth_env()), "FACTORY_GH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(key) or ""
        if len(value) >= 8 and not value.startswith("http"):
            text = text.replace(value, "[entfernt]")
    return text


def runtime_info() -> dict:
    """Where this process runs: shown on the board for every station."""
    container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    if os.environ.get("GITHUB_ACTIONS") == "true":
        hosted = os.environ.get("RUNNER_ENVIRONMENT", "") == "github-hosted"
        system = " ".join(x for x in (os.environ.get("RUNNER_OS"), os.environ.get("ImageOS"), os.environ.get("RUNNER_ARCH")) if x)
        return {"host": "github-hosted" if hosted else "self-hosted", "os": system, "container": container,
                "runner": os.environ.get("RUNNER_NAME", ""), "job": os.environ.get("GITHUB_JOB", ""),
                "sandboxed": sandboxed()}
    return {"host": "local", "os": f"{platform.system()} {platform.release()} {platform.machine()}",
            "container": container, "runner": "", "job": "", "sandboxed": sandboxed()}


def run_agent(
    cfg: dict, profile: dict, role: str, prompt: str, cwd: Path, *,
    model: str, effort: str | None, budget_usd: float, dirs: list[Path], plugin_dirs: list[Path],
    on_tool=None, on_event=None, env_extra: dict | None = None,
) -> AgentResult:
    rc = cfg["roles"][role]
    ac = cfg.get("agent", {})
    h = harnesses.for_role(cfg, role)
    box = sandboxed()
    if not box and "tool_allowlist" not in h.capabilities:
        return AgentResult(False, error=f"{h.label} cannot be limited to named commands, so it only runs on a "
                                        "sandboxed runner", model=model, harness=h.name)
    default = harnesses.name_for({"agent": ac}, role) == h.name   # agent.command names the default harness's CLI
    req = harnesses.Request(
        prompt=prompt, cwd=cwd, model=model, effort=effort, budget_usd=budget_usd, fallback=rc.get("fallback"),
        dirs=list(dirs), plugin_dirs=list(plugin_dirs), tools=rc.get("tools", "read"),
        agent_commands=list(profile.get("agent_commands") or []), sandboxed=box,
        command=(ac.get("command") if default else "") or "", settings=ac)
    env = {**clean_env(cfg, profile, h), **(env_extra or {})}
    rtk = rtks.find(cfg) if "shell_hook" in h.capabilities else ""
    if rtk:
        req.rtk_hook = rtks.hook_command()
        req.rtk_allowed = [] if box else rtks.allowed(rtk, h.shell_prefixes(req))
        env.update(FACTORY_RTK=rtk, FACTORY_RTK_ALLOW="*" if box else json.dumps(req.rtk_allowed))
        rtk_before = rtks.gain(rtk, cwd)
    cmd = h.argv(req)

    minutes = float(rc.get("timeout_minutes", 30))
    started = time.time()
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                cwd=str(cwd), env=env)
    except FileNotFoundError:
        return AgentResult(False, error=f"agent command not found: {cmd[0]}", model=model, harness=h.name)

    timed_out = threading.Event()

    def kill():
        timed_out.set()
        proc.kill()

    def send_prompt():   # in a thread, so a long prompt cannot block while the agent already writes output
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    timer = threading.Timer(minutes * 60, kill)
    timer.start()
    threading.Thread(target=send_prompt, daemon=True).start()
    stream = h.reader(cwd)
    try:
        for line in proc.stdout:
            for happened in stream.feed(line):
                if on_tool and happened["type"] == "tool":
                    on_tool(happened["tool"], happened["text"])
                if on_event:
                    on_event(happened)
        code = proc.wait()
    finally:
        timer.cancel()
    duration = time.time() - started

    end = stream.final(model)
    seen = dict(skills=stream.skills, turns=end.turns, tool_calls=sum(stream.tools.values()),
                subagents=stream.subagents, tools=stream.tools, tokens=end.tokens, context_peak=stream.context_peak,
                context_window=end.context_window, subscription=stream.subscription,
                cost_usd=end.cost_usd, duration_s=duration, model=model, harness=h.name,
                rtk=rtks.since(rtk_before, rtks.gain(rtk, cwd)) if rtk else {})
    if timed_out.is_set():
        return AgentResult(False, error=f"timed out after {minutes:g} minutes", **seen)
    if code != 0 or end.error or not end.done:
        detail = end.text or " | ".join(stream.noise[-4:])[-400:] or f"exit code {code}"
        return AgentResult(False, text=end.text, error=f"{end.kind or 'error'}: {detail}", **seen)
    return AgentResult(True, text=end.text, **seen)
