"""Every path that turns a tool NAME into a schema must record a miss.

The same swallow was written twice, in two places, under the same false comment
("graph validation will catch it"). `AgentRegistry.resolve_tool_schemas` was fixed
first; the CAPABILITY grant path in `core.py` kept discarding, and nothing downstream
covers it — `graph.validate()` sees only the YAML, and a capability's tool list is not
in the YAML at all. It is not reachable by the linter either, which checks the graph's
`tool_name` fields.

So a capability whose tool is missing grants nothing, silently, on the very path
`pipeline_forge`'s tool-build step uses (`capability: tool_creation` → write/run_tests/
pytest/register_tool). One helper now owns the question, and `unresolved_tools()`
answers it for every owner.
"""
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skillflow import SkillFlow


def _loader(known):
    m = MagicMock()

    def load_schema(name):
        if name not in known:
            raise ImportError(f"no tool {name}")
        return {"name": name}
    m.load_schema.side_effect = load_schema
    return m


def _sf(known):
    sf = SkillFlow(":memory:")
    sf._tool_loader = _loader(known)
    return sf


class TestCapabilityGrants:
    def test_a_missing_capability_tool_is_recorded(self):
        sf = _sf({"write"})
        assert sf._resolve_tool_schema("write", owner="capability:tool_creation")
        assert sf._resolve_tool_schema(
            "register_tool", owner="capability:tool_creation") is None
        assert sf.unresolved_tools() == {
            "capability:tool_creation": ["register_tool"]}

    def test_it_warns_once_per_owner_and_name(self, caplog):
        """The agent-config half of this was quadratic before it was bounded; the
        claim path runs on EVERY step, so an unconditional warning here would be
        far worse — one line per step for the life of the run."""
        sf = _sf(set())
        with caplog.at_level(logging.WARNING, logger="skillflow"):
            for _ in range(5):
                sf._resolve_tool_schema("nope", owner="capability:c")
        assert len([r for r in caplog.records if "does not resolve" in r.message]) == 1

    def test_the_record_clears_once_the_tool_appears(self):
        sf = _sf(set())
        sf._resolve_tool_schema("late", owner="capability:c")
        assert sf.unresolved_tools() == {"capability:c": ["late"]}
        sf._tool_loader = _loader({"late"})
        assert sf._resolve_tool_schema("late", owner="capability:c") == {"name": "late"}
        assert sf.unresolved_tools() == {}

    def test_no_loader_is_not_a_finding(self):
        """A host with no ToolLoader has not declared anything missing."""
        sf = SkillFlow(":memory:")
        sf._tool_loader = None
        assert sf._resolve_tool_schema("x", owner="capability:c") is None
        assert sf.unresolved_tools() == {}


class TestOneViewAcrossEveryGrantPath:
    def test_role_and_capability_misses_share_one_report(self):
        sf = _sf({"read_file"})
        sf.register_agent_config("maker", model="host",
                                 tools=["read_file", "write_file"])
        sf._resolve_tool_schema("register_tool", owner="capability:tool_creation")
        assert sf.unresolved_tools() == {
            "agent_config:maker": ["write_file"],
            "capability:tool_creation": ["register_tool"],
        }

    def test_a_clean_setup_reports_nothing(self):
        sf = _sf({"read_file", "write"})
        sf.register_agent_config("maker", model="host",
                                 tools=["read_file", "write"])
        assert sf.unresolved_tools() == {}
