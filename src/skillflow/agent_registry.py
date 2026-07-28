"""Agent registry — validates graph agent_config references exist.

Host apps register agent configs at startup.  The registry does NOT
know how to call LLMs — it only stores configs and resolves tool
schemas so the graph can be fully validated before any run starts.

Usage::

    sf = SkillFlow(":memory:")
    sf.register_agent_config("researcher", {
        "model": "deepseek/deepseek-v4-flash",
        "tools": ["read_file", "write", "list_tree"],
        "system_prompt": "You are a researcher...",
    })
    # Graph validation will now catch missing agent_config refs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("skillflow.agent_registry")


@dataclass
class AgentConfig:
    """Opaque config for an agent referenced by name in a graph step.

    skillflow never interprets these fields — they are passed through
    to the host's StepRunner implementation.  The only thing skillflow
    does is validate that the name exists when registering a graph.
    """

    name: str
    model: str = ""
    tools: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    # Resolved tool schemas (populated when tool_loader is available)
    tool_schemas: dict[str, dict] = field(default_factory=dict)
    # Declared tools that did not resolve — silently unavailable to this agent.
    unknown_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "tools": self.tools,
            "config": self.config,
            "tool_schemas": self.tool_schemas,
        }


class AgentRegistry:
    """Registry of agent configs indexed by name.

    Validates that every graph step's ``agent_config`` references a
    registered agent name.  Optionally resolves tool schemas from a
    ToolLoader so the host StepRunner receives everything it needs.
    """

    def __init__(self):
        self._configs: dict[str, AgentConfig] = {}

    # ── Registration ──────────────────────────────────────────

    def register(self, name: str, *,
                 model: str = "",
                 tools: list[str] | None = None,
                 **kwargs) -> AgentConfig:
        """Register an agent config.

        Extra kwargs become ``config`` entries (e.g. template, temperature,
        thinking settings — anything the host StepRunner needs).
        """
        cfg = AgentConfig(
            name=name,
            model=model,
            tools=tools or [],
            config=kwargs,
        )
        self._configs[name] = cfg
        return cfg

    def register_dict(self, name: str, d: dict) -> AgentConfig:
        """Register from a flat dict (convenience for YAML-loaded configs).

        ``model`` and ``tools`` are extracted; everything else goes into
        ``config``.
        """
        d = dict(d)
        model = d.pop("model", "")
        tools = d.pop("tools", [])
        return self.register(name, model=model, tools=tools, **d)

    # ── Query ─────────────────────────────────────────────────

    def get(self, name: str) -> AgentConfig | None:
        return self._configs.get(name)

    def list_names(self) -> list[str]:
        return list(self._configs.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._configs

    def __len__(self) -> int:
        return len(self._configs)

    # ── Tool schema resolution ────────────────────────────────

    def resolve_tool_schemas(self, tool_loader) -> None:
        """Resolve tool schemas for all registered agent configs.

        Called once after all configs and tools are registered.
        For each tool name in each agent config, loads the tool
        schema from the ToolLoader and caches it in tool_schemas.
        """
        for name, cfg in self._configs.items():
            cfg.tool_schemas = {}
            missing: list[str] = []
            for tool_name in cfg.tools:
                try:
                    cfg.tool_schemas[tool_name] = tool_loader.load_schema(tool_name)
                except ImportError:
                    # `load_schema` IS the existence check, and this used to discard
                    # the answer on the claim that "graph validation will catch" it.
                    # It does not: graph.validate() sees only the YAML, and an agent's
                    # tool list is not in the graph. A role granted `write_file` /
                    # `create_file` / `edit_file` therefore registered clean, ran
                    # without them, produced nothing, and still reported success.
                    # Record instead of swallow. Do NOT raise: this method re-resolves
                    # EVERY config on each register_agent_config* call, so a host that
                    # registers agents before tools would otherwise break — the list
                    # simply clears itself once the tool appears.
                    missing.append(tool_name)
            # Warn only when the picture CHANGES for this config. This method
            # re-resolves every registered config on every register_agent_config*
            # call, so an unconditional warning is quadratic: registering 10 roles
            # of which 5 are bad emitted ~90 identical lines and buried the run log.
            if missing and missing != cfg.unknown_tools:
                logger.warning(
                    "agent config %r declares tools that do not resolve: %s — "
                    "they are silently unavailable to that agent", name, missing)
            cfg.unknown_tools = missing

    def unknown_tools(self) -> dict[str, list[str]]:
        """``{agent_config: [tool names that do not resolve]}``.

        Populated by ``resolve_tool_schemas``. A host can surface this after
        registration — the tools are silently unavailable to those agents, which
        otherwise shows up only as an agent that mysteriously produces nothing.
        """
        return {name: list(cfg.unknown_tools)
                for name, cfg in self._configs.items() if cfg.unknown_tools}

    # ── Serialization ─────────────────────────────────────────

    def to_dict(self) -> dict[str, dict]:
        return {name: cfg.to_dict() for name, cfg in self._configs.items()}
