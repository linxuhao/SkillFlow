"""Tests for skillflow.write_tools."""

from skillflow.write_tools import generate_write_tool_schemas, resolve_write_target


class TestWriteTools:
    def test_content_mode_fixed_outputs(self):
        schemas = generate_write_tool_schemas(
            "content", {"sota": "step1_5_sota.md"}
        )
        # 3 tools per slot + finish_step
        assert len(schemas) == 4
        names = {s["name"] for s in schemas}
        assert names == {"write_sota", "create_sota", "append_sota", "finish_step"}
        # write_sota has "content" param
        write = next(s for s in schemas if s["name"] == "write_sota")
        assert "content" in write["parameters"]
        # create_sota has "initialContent" param
        create = next(s for s in schemas if s["name"] == "create_sota")
        assert "initialContent" in create["parameters"]

    def test_content_mode_glob_pattern(self):
        schemas = generate_write_tool_schemas(
            "content", {"task_card": "tasks/*.json"}
        )
        # 3 per-slot tools + finish_step
        assert len(schemas) == 4
        names = {s["name"] for s in schemas}
        assert names == {"write_task_card", "create_task_card", "append_task_card", "finish_step"}
        # glob tools have "id" param (skip finish_step which has summary instead)
        for tool in schemas:
            if tool["name"] == "finish_step":
                continue
            assert "id" in tool["parameters"]

    def test_content_mode_multiple_fixed(self):
        schemas = generate_write_tool_schemas("content", {
            "plan": "task_plan.md",
            "manifest": "subtasks_manifest.json",
        })
        # 3 tools × 2 slots + finish_step = 7
        assert len(schemas) == 7
        names = {s["name"] for s in schemas}
        assert names == {
            "write_plan", "create_plan", "append_plan",
            "write_manifest", "create_manifest", "append_manifest",
            "finish_step",
        }

    def test_write_mode_no_fixed(self):
        schemas = generate_write_tool_schemas("write", {})
        # write + finish_step
        assert len(schemas) == 2
        assert schemas[0]["name"] == "write"
        assert "file" in schemas[0]["parameters"]
        assert schemas[1]["name"] == "finish_step"

    def test_empty_mode(self):
        schemas = generate_write_tool_schemas("", {})
        assert schemas == []

    def test_resolve_write_target_simple(self):
        target = resolve_write_target("sota", {"sota": "step1_5_sota.md"}, {})
        assert target == "step1_5_sota.md"

    def test_resolve_write_target_glob(self):
        target = resolve_write_target(
            "task_card", {"task_card": "tasks/*.json"}, {"id": "core_lib"}
        )
        assert target == "tasks/core_lib.json"

    def test_resolve_write_target_missing(self):
        target = resolve_write_target("unknown", {}, {"file": "fallback.txt"})
        assert target == "fallback.txt"

    # ── format field tests ───────────────────────────────────────

    def test_format_in_description(self):
        schemas = generate_write_tool_schemas("content", {
            "verdict": {"file": "review_verdict.json", "on_exists": "new",
                        "format": '{"passed": bool, "feedback": str}'}
        })
        assert len(schemas) == 4
        # All three per-slot variants include format; finish_step does not
        for tool in schemas:
            if tool["name"] == "finish_step":
                continue
            desc = tool["description"]
            assert "Expected format:" in desc
            assert '{"passed": bool, "feedback": str}' in desc

    def test_no_format_in_description(self):
        schemas = generate_write_tool_schemas("content", {
            "sota": "step1_sota.md"
        })
        # First tool is write_sota
        desc = schemas[0]["description"]
        assert "Expected format:" not in desc
        assert desc == "Replace step1_sota.md with new content."

    def test_format_with_glob(self):
        schemas = generate_write_tool_schemas("content", {
            "task_card": {"file": "tasks/*.json",
                          "format": '{"id": str, "description": str}'}
        })
        # All per-slot tools include format hint; finish_step does not
        for tool in schemas:
            if tool["name"] == "finish_step":
                continue
            desc = tool["description"]
            assert "Expected format:" in desc
            assert '{"id": str, "description": str}' in desc
        # id param description links to content's id field when format has "id"
        write_tool = next(t for t in schemas if t["name"] == "write_task_card")
        id_desc = write_tool["parameters"]["id"]["description"]
        assert "Replaces * in tasks/*.json" in id_desc
        assert "must equal the 'id' field value" in id_desc

    def test_glob_id_param_without_id_in_format(self):
        """When format lacks 'id', the id param description stays plain."""
        schemas = generate_write_tool_schemas("content", {
            "items": {"file": "items/*.json",
                      "format": '{"name": str, "value": int}'}
        })
        write_tool = next(t for t in schemas if t["name"] == "write_items")
        id_desc = write_tool["parameters"]["id"]["description"]
        assert "Replaces * in items/*.json" in id_desc
        assert "must equal" not in id_desc  # no "id" in format → no hint

    def test_normalize_passes_format(self):
        from skillflow.write_tools import _normalize_fixed_entry
        # Dict entry preserves format
        result = _normalize_fixed_entry({"file": "x.json", "format": "schema"})
        assert result["format"] == "schema"
        # String entry omits format
        result = _normalize_fixed_entry("x.json")
        assert "format" not in result
