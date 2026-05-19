"""Tests for stepflow.workspace.WorkspaceManager."""

import pytest
from pathlib import Path
from stepflow.workspace import WorkspaceManager


@pytest.fixture
def ws(tmp_path):
    return WorkspaceManager(base_path=str(tmp_path / "workspaces"))


class TestWorkspaceManager:
    def test_base_path_created(self, ws):
        assert ws.base_path.exists()

    def test_get_project_path(self, ws):
        p = ws.get_project_path("proj-1")
        assert p.parent == ws.base_path
        assert p.name == "proj-1"

    def test_path_traversal_blocked(self, ws):
        with pytest.raises(PermissionError):
            ws.get_project_path("../../etc")

    def test_get_config_path(self, ws):
        p = ws.get_config_path("proj-1", "dpe_default")
        assert p.name == "dpe_default"
        assert p.exists()

    def test_get_inbox_dir(self, ws):
        p = ws.get_inbox_dir("proj-1", "dpe_default", "1")
        assert p.name == "Inbox_1"
        assert p.exists()

    def test_get_step_tmp_dir(self, ws):
        p = ws.get_step_tmp_dir("proj-1", "dpe_default", "t_impl")
        assert p.name == "t_impl.tmp"
        assert p.exists()

    def test_get_step_dir(self, ws):
        p = ws.get_step_dir("proj-1", "dpe_default", "2")
        assert p.name == "2"
        # step_dir is not auto-created

    def test_resolve_variables(self, ws):
        result = ws.resolve_variables(
            "proj-1", "dpe_default", "t_impl",
            {"source_dir": "$STEP_TMP_DIR"}
        )
        assert "t_impl.tmp" in result["source_dir"]
        # Backward compat
        result2 = ws.resolve_variables(
            "proj-1", "dpe_default", "t_impl",
            {"source_dir": "$STEP_DRAFT_DIR"}
        )
        assert "t_impl.tmp" in result2["source_dir"]

    def test_write_and_read_brief(self, ws):
        ws.write_brief("proj-1", "# Test Brief")
        content = ws.read_brief("proj-1")
        assert content == "# Test Brief"

    def test_read_output(self, ws):
        step_dir = ws.get_step_tmp_dir("proj-1", "dpe_default", "2")
        # Simulate step_commit: rename tmp → step_dir
        import os
        final = ws.get_step_dir("proj-1", "dpe_default", "2")
        os.rename(str(step_dir), str(final))
        (final / "design.md").write_text("Architecture design")
        content = ws.read_output("proj-1", "dpe_default", "2", "design.md")
        assert content == "Architecture design"

    def test_read_cross_config(self, ws):
        # Write in meta_conversation using step dir
        tmp = ws.get_step_tmp_dir("proj-1", "meta_conversation", "meta")
        import os
        final = ws.get_step_dir("proj-1", "meta_conversation", "meta")
        os.rename(str(tmp), str(final))
        (final / "brief.md").write_text("Project brief")
        # Read from dpe_default via cross-config
        content = ws.read_cross_config("proj-1", "meta_conversation", "brief.md")
        assert content == "Project brief"

    def test_read_nonexistent(self, ws):
        assert ws.read_brief("no-project") is None
        assert ws.read_output("no", "dp", "1", "none.md") is None
