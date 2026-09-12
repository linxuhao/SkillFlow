"""Agent-visible output contracts describe current destinations and operations."""
import json

import pytest

from skillflow.read_tools import generate_read_tool_schemas
from skillflow.write_tools import generate_write_tool_schemas


@pytest.mark.parametrize("target", ["artifact", "code"])
@pytest.mark.parametrize("mode,fixed", [
    ("write", {}),
    ("content", {"document": "document.md"}),
    ("content", {"document": "documents/*.md"}),
])
def test_output_descriptions_are_self_contained(target, mode, fixed):
    schemas = generate_write_tool_schemas(mode, fixed, output_target=target,
                                         allow_full_write=True)
    for schema in schemas:
        text = schema["description"]
        for obsolete in ("overlay", "repo_apply", "repo baseline"):
            assert obsolete not in text.lower()
        if schema["name"] == "finish_step":
            assert "validation" in text
            continue
        assert schema["output_target"] == target
        assert f"Destination: {target}" in text
        if target == "code":
            assert "worktree" in text and "repo-relative" in text
            assert "archived" not in text
        else:
            assert "step's staged candidate folder" in text
            assert "not promoted or published until" in text
            assert "source='self'" in text


def test_mixed_slots_keep_their_own_destination_and_document_shape():
    fixed = {
        "report": {"file": "verify_report.json", "target": "artifact",
                   "format": '{"passed": bool, "feedback": str}'},
        "readme": {"file": "README.md", "target": "code"},
    }
    schemas = {s["name"]: s for s in generate_write_tool_schemas("content", fixed)}
    for verb in ("write", "create", "edit"):
        assert "Destination: artifact" in schemas[f"{verb}_report"]["description"]
        assert "Destination: code" in schemas[f"{verb}_readme"]["description"]
        assert "archived" not in schemas[f"{verb}_readme"]["description"]
    assert set(schemas["write_report"]["parameters"]) == {"passed", "feedback"}
    assert "one argument per field" in schemas["write_report"]["description"]
    assert "archived" in schemas["create_report"]["description"]


def test_code_json_slot_preserves_structured_argument_instructions():
    schemas = {s["name"]: s for s in generate_write_tool_schemas(
        "content", {"config": {"file": "config.json", "target": "code",
                               "format": '{"enabled": bool}'}})}
    for name in ("write_config", "create_config"):
        assert schemas[name]["parameters"]["enabled"]["type"] == "boolean"
        assert "one argument per field" in schemas[name]["description"]


def test_artifact_revision_explains_preservation_and_explicit_removal():
    schemas = {s["name"]: s for s in generate_write_tool_schemas(
        "content", {"task": "tasks/*.json"}, carry_forward=True)}
    desc = schemas["delete_task"]["description"]
    assert "preserves unchanged artifacts" in desc
    assert "manifest" in desc and "Destination: artifact" in desc
    assert "staged candidate folder" in desc
    assert "not promoted or published until" in desc


def test_code_read_catalog_uses_current_worktree(tmp_path):
    schemas = generate_read_tool_schemas(
        [{"source_type": "repository", "mode": "tool"}], code_root=str(tmp_path))
    assert {s["name"] for s in schemas} == {"read", "search", "list"}
    for schema in schemas:
        text = json.dumps(schema, ensure_ascii=False).lower()
        assert "repo baseline" not in text
        assert "shadows" not in text
        assert "staging" not in text
        assert "current" in schema["parameters"]["source"]["description"]
