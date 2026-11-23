"""Harnesses: the agent CLIs a station can run in.

A harness adapter knows three things about its CLI: how to start one headless
run, how to read what the run prints, and what the CLI can do on its own. All
the rest (timeout, scrubbed environment, session log, contracts, budget
bookkeeping) lives in agent.py and works the same for every harness.

Adding a harness means one module here with a class like claude.Claude, and one
line in _ADAPTERS. A role picks its harness with roles.<role>.harness; without
one it runs in agent.harness, and without that in Claude Code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT = "claude"

# What an adapter can promise. The runner and the stations ask before they rely on one.
CAPABILITIES = {
    "budget_cap": "stops a run itself once it has cost the given amount",
    "fallback_model": "switches to a second model inside one run when the first is overloaded",
    "effort": "takes a reasoning-effort setting",
    "tool_allowlist": "can run without a sandbox, limited to named commands",
    "plugins": "loads skill packs from plugin directories",
    "skill_tool": "has a tool that invokes a skill by name, so the run reports which skills it used",
    "slash_commands": "can start from a /plugin:command entry",
    "cost": "reports what a run cost in USD",
    "subscription": "reports how much of a subscription limit a run used",
    "shell_hook": "runs a hook before each shell command, so RTK can shorten its output and heavy commands wait for a slot",
}


@dataclass
class Request:
    """One headless run, said without any CLI's words."""
    prompt: str
    cwd: Path
    model: str
    effort: str | None = None
    budget_usd: float = 1.0
    fallback: str | None = None
    dirs: list[Path] = field(default_factory=list)          # further directories the run may read and write
    plugin_dirs: list[Path] = field(default_factory=list)   # skill packs to load
    tools: str = "read"                                     # read | write | run
    agent_commands: list[str] = field(default_factory=list)  # project commands allowed for write and run
    sandboxed: bool = False                                 # a throwaway runner: no permission prompts needed
    command: str = ""                                       # the CLI to start; empty means the adapter's own
    settings: dict = field(default_factory=dict)            # the agent section of the configuration
    rtk_hook: str = ""                                      # the factory's hook before each shell call: RTK and the slots for heavy commands
    rtk_allowed: list[str] = field(default_factory=list)    # what the hook may rewrite to, allowed as well: rtk forms, the gate


@dataclass
class Final:
    """What a reader took from a finished run."""
    done: bool = False             # the CLI reported a final result
    error: bool = False            # ... and that result is a failure
    kind: str = ""                 # what sort of failure, in the CLI's words
    text: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    tokens: dict = field(default_factory=dict)   # input, output, cache_read, cache_write
    context_window: int = 0


class Reader:
    """Reads one run's output line by line. feed() returns what happened in that line, in order:
    what the agent said ("text"), every tool it called ("tool") and what came back ("result")."""

    def __init__(self, cwd: Path | str = ""):
        self.cwd = str(cwd)
        self.skills: list[str] = []        # skills the agent invoked, each once, in order of first use
        self.tools: dict[str, int] = {}    # tool name -> number of calls
        self.subagents = 0
        self.noise: list[str] = []         # lines that are not the CLI's format: usually an error message
        self.context_peak = 0
        self.subscription: dict[str, dict] = {}

    def feed(self, line: str) -> list[dict]:
        raise NotImplementedError

    def final(self, model: str) -> Final:
        raise NotImplementedError


class Harness:
    name = ""
    label = ""                          # shown on the settings page
    command = ""                        # the CLI it starts
    package = ""                        # what the runner installs to get that CLI
    capabilities: frozenset = frozenset()
    auth_env: frozenset = frozenset()   # variables that carry the model credentials
    env_keep: frozenset = frozenset()   # further variables the CLI needs, such as its config directory
    secrets: dict = {}                  # repository secrets a project needs for this harness -> purpose
    models: list = []                   # model names offered on the settings page

    def argv(self, req: Request) -> list[str]:
        raise NotImplementedError

    def reader(self, cwd: Path | str = "") -> Reader:
        raise NotImplementedError

    def shell_prefixes(self, req: Request) -> list[str]:
        """The shell commands a run without a sandbox may start (by prefix); empty when it cannot be limited."""
        return []

    def skill_instruction(self, chosen: bool = False) -> str:
        """The sentence that tells the agent how to use the skills a station offers. chosen: the station
        says when each skill applies, so the agent reads only the ones its work reaches."""
        if chosen:
            return ("A skill with a condition is read only when this run reaches it; skip it otherwise, since each "
                    "one read costs context. A skill without a condition is how this station works: read it and "
                    "follow it. The pipeline records which skills a run actually used.\n")
        return ("These skills are how this station is meant to work. Read each one at the point in the work it is "
                "meant for, and follow it. The pipeline records which skills a run actually used.\n")

    def entry(self, entry: str, args: str) -> str:
        """How a station's entry point opens the prompt."""
        return f"Start with {entry} {args}".rstrip()


def _adapters() -> dict[str, Harness]:
    from .claude import Claude
    return {h.name: h for h in (Claude(),)}


_ADAPTERS: dict[str, Harness] | None = None


def all_harnesses() -> dict[str, Harness]:
    global _ADAPTERS
    if _ADAPTERS is None:
        _ADAPTERS = _adapters()
    return _ADAPTERS


def get(name: str) -> Harness:
    try:
        return all_harnesses()[name]
    except KeyError:
        raise KeyError(f"unknown harness '{name}' (known: {', '.join(all_harnesses())})") from None


def name_for(cfg: dict, role: str) -> str:
    return (cfg.get("roles", {}).get(role) or {}).get("harness") or cfg.get("agent", {}).get("harness") or DEFAULT


def for_role(cfg: dict, role: str) -> Harness:
    return get(name_for(cfg, role))


def used(cfg: dict) -> list[str]:
    """Every harness the configuration runs a role in, the default first."""
    names = [cfg.get("agent", {}).get("harness") or DEFAULT]
    names += [name_for(cfg, role) for role in cfg.get("roles", {}) if role != "escalation"]
    return list(dict.fromkeys(names))


def install(cfg: dict, roles) -> list[str]:
    """What a runner installs so that these roles can run: one npm package@version per harness.
    agent.versions.<harness> pins a harness; agent.version pins the default one."""
    agent = cfg.get("agent", {})
    versions = agent.get("versions") or {}
    default = agent.get("harness") or DEFAULT
    out = []
    for name in dict.fromkeys(name_for(cfg, role) for role in roles):
        h = get(name)
        if h.package:
            version = versions.get(name) or (agent.get("version") if name == default else None) or "latest"
            out.append(f"{h.package}@{version}")
    return out


def all_auth_env() -> set[str]:
    return set().union(*(h.auth_env for h in all_harnesses().values()))


def problems(cfg: dict) -> list[str]:
    """Configuration lines that name a harness that does not exist."""
    known = all_harnesses()
    found = []
    if (cfg.get("agent") or {}).get("harness") not in (None, *known):
        found.append(f"agent.harness: '{cfg['agent']['harness']}' is not one of the harnesses ({', '.join(known)})")
    for role, rc in (cfg.get("roles") or {}).items():
        if (rc or {}).get("harness") not in (None, *known):
            found.append(f"roles.{role}.harness: '{rc['harness']}' is not one of the harnesses ({', '.join(known)})")
    return found
