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
