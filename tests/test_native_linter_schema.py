"""Native/plugin catalog schemas and the actual registered linter stay usable."""

from copy import deepcopy

import pytest
import yaml

from skillflow import SkillFlow
from skillflow.tool_loader import ToolLoader


@pytest.fixture
def native_loader():
    # Use the default SDK constructor, which registers native AND plugin tools.
    sf = SkillFlow()
    try:
        assert isinstance(sf._tool_loader, ToolLoader)
        yield sf._tool_loader
    finally:
        sf._conn.close()


def test_every_advertised_native_and_plugin_schema_loads(native_loader):
    names = native_loader.list_tools()
    assert names and "skillflow_lint" in names
    for name in names:
        schema = native_loader.load_schema(name)
        assert isinstance(schema, dict), name
    description = native_loader.load_schema("skillflow_lint")["description"]
    assert isinstance(description, str)
    assert "Returns {passed: bool" in description


def test_registered_native_linter_accepts_valid_and_rejects_missing_target(native_loader):
    # Resolve schema before callable, as normal tool consumers do.
    schema = native_loader.load_schema("skillflow_lint")
    assert schema["name"] == "skillflow_lint"
    lint = native_loader.load_fn("skillflow_lint")
    valid = {
        "name": "native_linter_control",
        "begin": "inspect",
        "end_conditions": {
            "combinator": "or",
            "conditions": [
                {"type": "node_reached", "node": "done", "result": "completed"},
            ],
        },
        "steps": [
            {
                "id": "inspect",
                "step_type": "tool",
                "tool_name": "read_file",
                "tool_params": {"file": "README.md"},
                "transitions": [{"to": "done"}],
            },
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }
    accepted = lint(content=yaml.safe_dump(valid), tool_loader=native_loader)
    assert accepted["passed"] is True, accepted
    assert accepted["errors"] == 0, accepted
    assert accepted["issues"] == [], accepted

    invalid = deepcopy(valid)
    invalid["steps"][0]["transitions"][0]["to"] = "absent_target"
    refused = lint(content=yaml.safe_dump(invalid), tool_loader=native_loader)
    assert refused["passed"] is False, refused
    assert refused["errors"] > 0, refused
    missing_target = [
        issue for issue in refused["issues"]
        if issue["severity"] == "error" and "absent_target" in issue["message"]
    ]
    assert missing_target, refused
    assert any(issue["suggestion"] for issue in missing_target), refused
