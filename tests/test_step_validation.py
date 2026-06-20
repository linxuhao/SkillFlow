"""Tests for skillflow.step_validation.StepValidator."""

import json
import pytest
from pathlib import Path
from skillflow.tool_loader import ToolLoader
from skillflow.step_validation import StepValidator


@pytest.fixture
def validator_with_tools(tmp_path):
    """Create a StepValidator backed by a ToolLoader with json_schema tool."""
    tools_dir = tmp_path / "tools"
    json_dir = tools_dir / "json_schema"
    json_dir.mkdir(parents=True)

    # Use the real json_schema tool from skillflow/tools/
    import shutil
    real_tool = Path(__file__).parent.parent.parent / "src" / "skillflow" / "tools" / "json_schema"
    if real_tool.exists():
        shutil.copytree(real_tool, json_dir, dirs_exist_ok=True)
    else:
        # Fallback: minimal tool
        (json_dir / "tool.yaml").write_text("name: json_schema")
        (json_dir / "impl.py").write_text("""
import json as _json
from pathlib import Path as _Path

def json_schema(files, inline_schema, *, workspace_root=""):
    root = _Path(workspace_root)
    results = []
    all_passed = True
    for pattern in files:
        for f in root.rglob(pattern):
            if not f.exists():
                continue
            try:
                data = _json.loads(f.read_text())
                required = inline_schema.get("required", [])
                for field in required:
                    if field not in data:
                        raise ValueError(f"Missing required field: {field}")
                results.append({"file": str(f.relative_to(root)), "passed": True, "error_message": ""})
            except Exception as e:
                all_passed = False
                results.append({"file": str(f.relative_to(root)), "passed": False, "error_message": str(e)})
    return {"all_passed": all_passed, "results": results}
""")

    loader = ToolLoader(tools_dir)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return StepValidator(loader, workspace)


class TestStepValidator:
    def test_single_validation_pass(self, validator_with_tools, tmp_path):
        ws = validator_with_tools._workspace_root
        (ws / "test.json").write_text(json.dumps({"name": "test", "value": 1}))

        result = validator_with_tools.validate([{
            "files": ["test.json"],
            "tool": "json_schema",
            "inline_schema": {"required": ["name"]}
        }])
        assert result["passed"] is True

    def test_single_validation_fail(self, validator_with_tools, tmp_path):
        ws = validator_with_tools._workspace_root
        (ws / "bad.json").write_text(json.dumps({"foo": 1}))

        result = validator_with_tools.validate([{
            "files": ["bad.json"],
            "tool": "json_schema",
            "inline_schema": {"required": ["name"]}
        }])
        assert result["passed"] is False
        assert len(result["errors"]) > 0

    def test_multiple_validations(self, validator_with_tools, tmp_path):
        ws = validator_with_tools._workspace_root
        (ws / "a.json").write_text(json.dumps({"name": "a"}))
        (ws / "b.json").write_text(json.dumps({"name": "b"}))

        result = validator_with_tools.validate([
            {"files": ["a.json"], "tool": "json_schema",
             "inline_schema": {"required": ["name"]}},
            {"files": ["b.json"], "tool": "json_schema",
             "inline_schema": {"required": ["name"]}},
        ])
        assert result["passed"] is True

    def test_validation_nonexistent_tool(self, validator_with_tools):
        result = validator_with_tools.validate([{
            "files": ["test.json"],
            "tool": "nonexistent_tool",
        }])
        assert result["passed"] is False

    def test_validation_empty_specs(self, validator_with_tools):
        result = validator_with_tools.validate([])
        assert result["passed"] is True

class TestStepValidatorEdgeCases:
    def test_validation_tool_not_found(self, validator_with_tools):
        result = validator_with_tools.validate([{
            "files": ["test.json"],
            "tool": "nonexistent_tool_xyz",
        }])
        assert result["passed"] is False

    def test_validation_tool_raises_exception(self, validator_with_tools, tmp_path):
        """Tool function raises an exception — should be caught."""
        ws = validator_with_tools._workspace_root
        (ws / "bad.json").write_text("not valid json {{{")

        result = validator_with_tools.validate([{
            "files": ["bad.json"],
            "tool": "json_schema",
            "inline_schema": {"required": ["passed"]},
        }])
        assert result["passed"] is False

    def test_validation_result_list_format(self, validator_with_tools):
        """Validator handles results as list (not dict with all_passed)."""
        result = validator_with_tools.validate([])
        assert result["passed"] is True

    def test_validation_missing_tool_key(self, validator_with_tools):
        """Spec without 'tool' key is skipped silently."""
        result = validator_with_tools.validate([{"files": ["x.json"]}])
        assert result["passed"] is True


class TestStepValidatorFileExists:
    """file_exists is the validation tool used by graph configs to verify
    step outputs exist (e.g. researcher step validates step1_sota.md).
    A missing file must produce {passed: False, errors: [...]} so the
    framework retries the step — this is the signal that triggers the
    infinite retry loop when the LLM doesn't call the write tool."""

    @pytest.fixture
    def validator_with_file_exists(self, tmp_path):
        from skillflow.tool_loader import ToolLoader
        import skillflow as sf_pkg
        native_dir = Path(sf_pkg.__file__).parent / "tools"
        loader = ToolLoader(native_dir)
        ws = tmp_path / "workspace"
        ws.mkdir()
        from skillflow.step_validation import StepValidator
        return StepValidator(loader, ws)

    def test_existing_file_passes(self, validator_with_file_exists, tmp_path):
        ws = validator_with_file_exists._workspace_root
        (ws / "step1_sota.md").write_text("# SOTA Report")

        result = validator_with_file_exists.validate([{
            "files": ["step1_sota.md"],
            "tool": "file_exists",
        }])
        assert result["passed"] is True

    def test_missing_file_fails_with_error(self, validator_with_file_exists):
        result = validator_with_file_exists.validate([{
            "files": ["step1_sota.md"],
            "tool": "file_exists",
        }])
        assert result["passed"] is False
        assert len(result["errors"]) >= 1
        assert "File not found" in result["errors"][0].get("error_message", "")

    def test_mixed_existing_and_missing(self, validator_with_file_exists, tmp_path):
        ws = validator_with_file_exists._workspace_root
        (ws / "existing.json").write_text("{}")

        result = validator_with_file_exists.validate([
            {"files": ["existing.json"], "tool": "file_exists"},
            {"files": ["missing.md"], "tool": "file_exists"},
        ])
        assert result["passed"] is False


class TestMaxRetriesFiltering:
    """max_retries in a validation/lifecycle spec is a lifecycle control param,
    NOT a tool parameter — it must not leak into the tool function call."""

    def test_max_retries_not_passed_to_lint(self, tmp_path):
        from skillflow.step_validation import StepValidator
        from skillflow.tool_loader import ToolLoader

        (tmp_path / "test.py").write_text("x = 1\n")

        tools_dir = Path(__file__).parent.parent / "src" / "skillflow" / "tools"
        loader = ToolLoader(tools_dir)
        validator = StepValidator(loader, tmp_path)

        spec = {
            "tool": "lint",
            "files": ["test.py"],
            "workspace_root": str(tmp_path),
            "on_failure": "retry",
            "max_retries": 3,  # lifecycle control, NOT a lint param
        }
        # Must not raise TypeError: unexpected keyword argument 'max_retries'
        result = validator.validate([spec])
        assert "passed" in result

    def test_max_retries_not_passed_to_file_exists(self, tmp_path):
        from skillflow.step_validation import StepValidator
        from skillflow.tool_loader import ToolLoader

        (tmp_path / "output.json").write_text("{}")

        tools_dir = Path(__file__).parent.parent / "src" / "skillflow" / "tools"
        loader = ToolLoader(tools_dir)
        validator = StepValidator(loader, tmp_path)

        spec = {
            "tool": "file_exists",
            "files": ["output.json"],
            "on_failure": "warn",
            "max_retries": 5,
        }
        result = validator.validate([spec])
        assert "passed" in result
        assert result["passed"] is True
