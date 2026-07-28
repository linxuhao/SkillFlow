"""skillflow owns the tool registry, so tool EXISTENCE is its question to answer.

Two places used to know the answer and throw it away:

* `AgentRegistry.resolve_tool_schemas` calls `load_schema` on every tool a role
  declares — that IS the existence check — and swallowed the ImportError under the
  comment "graph validation will catch". It does not: `graph.validate()` sees only the
  YAML, and an agent's tool list is not in the graph. A role granted `write_file` /
  `create_file` / `edit_file` registered clean, ran without them, produced nothing, and
  still reported success.
* The linter had no ToolLoader at all, so it was purely structural and a graph naming a
  tool that does not exist linted clean.

Neither check judges WHICH tools a role should have — that is the config author's call.
"""
from unittest.mock import MagicMock

from skillflow.agent_registry import AgentRegistry
from skillflow.plugins.linter import lint_content


def _loader(known):
    m = MagicMock()

    def load_schema(name):
        if name not in known:
            raise ImportError(f"no tool {name}")
        return {"name": name}
    m.load_schema.side_effect = load_schema
    return m


class TestRoleToolExistence:
    def test_unresolvable_tools_are_recorded_not_swallowed(self):
        reg = AgentRegistry()
        reg.register("maker", model="host",
                     tools=["read_file", "write_file", "create_file"])
        reg.resolve_tool_schemas(_loader({"read_file"}))

        assert reg.unknown_tools() == {"maker": ["write_file", "create_file"]}
        assert list(reg.get("maker").tool_schemas) == ["read_file"]

    def test_a_fully_resolvable_config_reports_nothing(self):
        reg = AgentRegistry()
        reg.register("maker", model="host", tools=["read_file", "write"])
        reg.resolve_tool_schemas(_loader({"read_file", "write"}))
        assert reg.unknown_tools() == {}

    def test_registration_still_succeeds(self):
        """Raising would break hosts that register agents before tools."""
        reg = AgentRegistry()
        reg.register("maker", model="host", tools=["not_yet_built"])
        reg.resolve_tool_schemas(_loader(set()))          # must not raise
        assert "maker" in reg

    def test_the_record_clears_once_the_tool_appears(self):
        """`resolve_tool_schemas` re-resolves EVERY config on each registration, so a
        tool registered after its agent must clear the finding rather than strand it."""
        reg = AgentRegistry()
        reg.register("maker", model="host", tools=["late_tool"])
        reg.resolve_tool_schemas(_loader(set()))
        assert reg.unknown_tools() == {"maker": ["late_tool"]}

        reg.resolve_tool_schemas(_loader({"late_tool"}))
        assert reg.unknown_tools() == {}

    def test_it_does_not_judge_which_tools_a_role_should_have(self):
        """Existence only. A role granted something odd but real is fine."""
        reg = AgentRegistry()
        reg.register("reviewer", model="host", tools=["draft_commit"])
        reg.resolve_tool_schemas(_loader({"draft_commit"}))
        assert reg.unknown_tools() == {}


_GRAPH = """
name: g
begin: work
end_conditions:
  combinator: or
  conditions:
    - {type: node_reached, node: done, result: completed}
steps:
  - id: work
    step_type: tool
    tool_name: totally_not_a_tool
    transitions:
      - {to: done}
  - id: done
    step_type: gate
    transitions:
      - {to: null}
"""


class TestLinterToolExistence:
    def test_without_a_loader_the_lint_stays_structural(self):
        """Back-compat: the old call signature behaves exactly as before."""
        issues = lint_content(_GRAPH)
        assert not [i for i in issues if "tool registry" in i.message]

    def test_with_a_loader_an_invented_tool_is_an_error(self):
        issues = lint_content(_GRAPH, tool_loader=_loader({"write"}))
        bad = [i for i in issues if "tool registry" in i.message]
        assert bad and "totally_not_a_tool" in bad[0].message
        assert bad[0].severity == "error"

    def test_a_real_tool_passes(self):
        issues = lint_content(_GRAPH.replace("totally_not_a_tool", "write"),
                              tool_loader=_loader({"write"}))
        assert not [i for i in issues if "tool registry" in i.message]


def test_the_warning_does_not_repeat_on_every_registration(caplog):
    """`resolve_tool_schemas` re-resolves EVERY config on EVERY registration, so an
    unconditional warning is quadratic — 10 roles with 5 bad ones emitted ~90 identical
    lines and buried the run log. Warn when the picture changes, not on every pass."""
    import logging
    reg = AgentRegistry()
    reg.register("maker", model="host", tools=["nope"])
    loader = _loader(set())
    with caplog.at_level(logging.WARNING, logger="skillflow.agent_registry"):
        for _ in range(5):
            reg.resolve_tool_schemas(loader)
    assert len([r for r in caplog.records if "do not resolve" in r.message]) == 1
    assert reg.unknown_tools() == {"maker": ["nope"]}
