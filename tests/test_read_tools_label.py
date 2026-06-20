"""Tests for _derive_label $var resolution — ensures loop variables like
$current_task are resolved in read tool names, not left as literal strings."""
import pytest
from skillflow.read_tools import _derive_label


class TestDeriveLabel:
    def test_unresolved_dollar_current_task(self):
        """Without loop_context, $current_task stays in the label (backward compat)."""
        spec = {
            "source_type": "step",
            "step_id": "3",
            "files": ["tasks/$current_task.json"],
        }
        label = _derive_label(spec)
        assert label == "step_3_$current_task"

    def test_resolved_current_task(self):
        """With loop_context, $current_task is resolved to the actual task name."""
        spec = {
            "source_type": "step",
            "step_id": "3",
            "files": ["tasks/$current_task.json"],
        }
        loop_context = {
            "current_task": "backend_routes_modify",
            "[current_task]": "backend_routes_modify",
        }
        label = _derive_label(spec, loop_context)
        assert label == "step_3_backend_routes_modify"

    def test_resolved_via_bracket_key(self):
        """loop_context may only have the [bracket] form from skillflow."""
        spec = {
            "source_type": "step",
            "step_id": "3",
            "files": ["tasks/$current_task.json"],
        }
        loop_context = {"[current_task]": "frontend_product"}
        label = _derive_label(spec, loop_context)
        assert label == "step_3_frontend_product"

    def test_no_files_returns_step_only(self):
        """When no files specified, label is just step_N."""
        spec = {"source_type": "step", "step_id": "3", "files": []}
        label = _derive_label(spec, {"current_task": "x"})
        assert label == "step_3"

    def test_multiple_files_returns_step_only(self):
        """When multiple files, label is just step_N (no single file to name)."""
        spec = {
            "source_type": "step",
            "step_id": "3",
            "files": ["tasks/$current_task.json", "tasks/another.json"],
        }
        label = _derive_label(spec, {"current_task": "x"})
        assert label == "step_3"

    def test_no_dollar_in_filename(self):
        """Without $var, label uses file stem directly."""
        spec = {
            "source_type": "step",
            "step_id": "2",
            "files": ["step2_design.md"],
        }
        label = _derive_label(spec)
        assert label == "step_2_step2_design"

    def test_config_source_type(self):
        """Config-type specs also resolve $var."""
        spec = {
            "source_type": "config",
            "config_name": "dpe",
            "step_id": "3",
            "files": ["tasks/$current_task.json"],
        }
        label = _derive_label(spec, {"current_task": "my_task"})
        assert label == "config_dpe_3_my_task"


class TestResolveVarPath:
    """Tests for _resolve_var_path — shared helper for $var resolution."""

    def test_resolves_dollar_var(self):
        from skillflow.read_tools import _resolve_var_path
        ctx = {"current_task": "backend_sessions_api"}
        result = _resolve_var_path("tasks/$current_task.json", ctx)
        assert result == "tasks/backend_sessions_api.json"

    def test_resolves_via_bracket_key(self):
        from skillflow.read_tools import _resolve_var_path
        ctx = {"[current_task]": "my_task"}
        result = _resolve_var_path("tasks/$current_task.json", ctx)
        assert result == "tasks/my_task.json"

    def test_no_dollar_returns_unchanged(self):
        from skillflow.read_tools import _resolve_var_path
        result = _resolve_var_path("tasks/static.json", {"x": "y"})
        assert result == "tasks/static.json"

    def test_no_loop_context_returns_unchanged(self):
        from skillflow.read_tools import _resolve_var_path
        result = _resolve_var_path("tasks/$current_task.json", None)
        assert result == "tasks/$current_task.json"

    def test_empty_loop_context_returns_unchanged(self):
        from skillflow.read_tools import _resolve_var_path
        result = _resolve_var_path("tasks/$current_task.json", {})
        assert result == "tasks/$current_task.json"

    def test_multiple_vars_in_path(self):
        from skillflow.read_tools import _resolve_var_path
        ctx = {"step": "3", "task": "my_task"}
        result = _resolve_var_path("$step/tasks/$task.json", ctx)
        assert result == "3/tasks/my_task.json"


class TestResolveContextPathsVarResolution:
    """resolve_context_paths with $var files must resolve via loop_context
    to find existing files, instead of the broken $→* fallback."""

    def test_resolves_dollar_var_to_existing_file(self, tmp_path):
        """$current_task.json → backend_sessions_api.json (which exists)."""
        from skillflow.read_tools import resolve_context_paths

        # Setup: create step dir with a task file
        step_dir = tmp_path / "dpe_default_v2" / "3" / "tasks"
        step_dir.mkdir(parents=True)
        (step_dir / "backend_sessions_api.json").write_text('{"key": "val"}')

        spec = {
            "source_type": "step",
            "step_id": "3",
            "files": ["tasks/$current_task.json"],
        }
        loop_context = {"current_task": "backend_sessions_api"}

        paths = resolve_context_paths(
            spec, str(tmp_path), "dpe_default_v2", loop_context=loop_context
        )
        assert len(paths) == 1
        assert paths[0].endswith("backend_sessions_api.json")

    def test_unresolved_dollar_var_falls_back_to_glob(self, tmp_path):
        """When $var not in loop_context, fall back to glob matching."""
        from skillflow.read_tools import resolve_context_paths

        step_dir = tmp_path / "dpe_default_v2" / "3" / "tasks"
        step_dir.mkdir(parents=True)
        (step_dir / "backend_sessions_api.json").write_text("{}")

        spec = {
            "source_type": "step",
            "step_id": "3",
            "files": ["tasks/$unknown_var.json"],
        }
        # No matching key in loop_context → $unknown_var stays → glob fallback
        loop_context = {"other": "val"}

        paths = resolve_context_paths(
            spec, str(tmp_path), "dpe_default_v2", loop_context=loop_context
        )
        # glob with * in place of $unknown_var should match the file
        assert len(paths) == 1
        assert paths[0].endswith(".json")
