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

from dataclasses import dataclass, field
from typing import Any


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
        for cfg in self._configs.values():
            cfg.tool_schemas = {}
            for tool_name in cfg.tools:
                try:
                    cfg.tool_schemas[tool_name] = tool_loader.load_schema(tool_name)
                except ImportError:
                    pass  # tool not found — graph validation will catch

    # ── Serialization ─────────────────────────────────────────

    def to_dict(self) -> dict[str, dict]:
        return {name: cfg.to_dict() for name, cfg in self._configs.items()}
