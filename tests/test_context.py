from pathlib import Path
"""Tests for skillflow.context.ContextResolver."""

import pytest
from pathlib import Path
from skillflow.context import ContextResolver


@pytest.fixture
def workspace(tmp_path):
    """Create a mock workspace structure with new-style step directories."""
    ws = tmp_path / "workspace"

    # Cross-config: meta_conversation output
    meta = ws / "meta_conversation" / "meta"
    meta.mkdir(parents=True)
    (meta / "brief.md").write_text("# Project Brief\nTest project brief content")

    # Same-config: dpe_default outputs in per-step dirs
    dpe = ws / "dpe_default"
    (dpe / "2").mkdir(parents=True)
    (dpe / "2" / "step2_design.md").write_text(
        "# Architecture\n## Overview\nTest design\n## Interface\n- GET /api\n- POST /data\n## Notes\nExtra info"
    )

    (dpe / "1").mkdir(parents=True)
    (dpe / "1" / "step1_sota.md").write_text(
        "# SOTA Report\n" + "\n".join(f"line {i}" for i in range(200))
    )

    return ws


class TestContextResolver:
    def test_cross_config_source(self, workspace):
        resolver = ContextResolver(workspace)
        specs = [{"source": {"config": "meta_conversation", "output": "brief.md"}}]
        result = resolver.resolve(specs, current_config="dpe_default")
        assert len(result) == 1
        content = list(result.values())[0]
        assert "Project Brief" in content

    def test_previous_step_source(self, workspace):
        resolver = ContextResolver(workspace)
        specs = [{"source": {"step": "2", "output": "step2_design.md"}}]
        result = resolver.resolve(specs, current_config="dpe_default")
        assert len(result) == 1
        content = list(result.values())[0]
        assert "Architecture" in content

    def test_mode_summary(self, workspace):
        resolver = ContextResolver(workspace)
        specs = [{"source": {"step": "1", "output": "step1_sota.md",
                             "mode": "summary"}}]
        result = resolver.resolve(specs, current_config="dpe_default")
        content = list(result.values())[0]
        lines = content.splitlines()
        assert len(lines) <= 102  # 100 lines + "... [summary truncated]"

    def test_mode_interfaces(self, workspace):
        resolver = ContextResolver(workspace)
        specs = [{"source": {"step": "2", "output": "step2_design.md",
                             "mode": "interfaces"}}]
        result = resolver.resolve(specs, current_config="dpe_default")
        content = list(result.values())[0]
        assert "GET /api" in content
        assert "Overview" not in content  # Non-interface section excluded

    def test_multiple_sources(self, workspace):
        resolver = ContextResolver(workspace)
        specs = [
            {"source": {"config": "meta_conversation", "output": "brief.md"}},
            {"source": {"step": "2", "output": "step2_design.md"}},
        ]
        result = resolver.resolve(specs, current_config="dpe_default")
        assert len(result) == 2

    def test_nonexistent_source_returns_empty(self, workspace):
        resolver = ContextResolver(workspace)
        specs = [{"source": {"step": "nonexistent", "output": "none.md"}}]
        result = resolver.resolve(specs, current_config="dpe_default")
        assert len(result) == 0

class TestContextResolverEdgeCases:
    def test_cross_config_with_specific_step(self, workspace):
        from skillflow.context import ContextResolver
        resolver = ContextResolver(workspace)
        specs = [{"source": {"config": "meta_conversation", "step": "meta",
                              "output": "brief.md"}}]
        result = resolver.resolve(specs, current_config="dpe_default")
        assert len(result) == 1

    def test_cross_config_nonexistent_config(self, workspace):
        from skillflow.context import ContextResolver
        resolver = ContextResolver(workspace)
        specs = [{"source": {"config": "nonexistent", "output": "brief.md"}}]
        result = resolver.resolve(specs, current_config="dpe_default")
        assert len(result) == 0

    def test_step_output_file_not_found(self, workspace):
        from skillflow.context import ContextResolver
        resolver = ContextResolver(workspace)
        specs = [{"source": {"step": "2", "output": "nonexistent.md"}}]
        result = resolver.resolve(specs, current_config="dpe_default")
        assert len(result) == 0

    def test_tool_source_no_loader(self, workspace):
        from skillflow.context import ContextResolver
        resolver = ContextResolver(workspace)  # no tool_loader
        specs = [{"source": {"tool": "dir_tree"}}]
        result = resolver.resolve(specs, current_config="dpe_default")
        assert isinstance(result, dict)

    def test_extract_interfaces_fallback(self):
        from skillflow.context import ContextResolver
        text = "# Miscellaneous Notes\nJust some notes."
        extracted = ContextResolver._extract_interfaces(text)
        assert "no interface sections found" in extracted.lower()

    def test_empty_source(self):
        from skillflow.context import ContextResolver
        resolver = ContextResolver(Path("/nonexistent"))
        result = resolver.resolve([{}], current_config="")
        assert result == {}


class TestVolatilityOrdering:
    """Cache-stability tiering: static reads emitted before volatile sources."""

    def test_tier_classification(self):
        t = ContextResolver._volatility_tier
        assert t({"config": "meta", "output": "b.md"}) == 0
        assert t({"source_type": "repository"}) == 0
        assert t({"step": "1"}) == 1
        assert t({"source_type": "step"}) == 1
        assert t({"tool": "dir_tree"}) == 2
        assert t({"source_type": "workspace"}) == 2

    def test_resolve_emits_static_before_step(self, workspace):
        resolver = ContextResolver(workspace)
        # Declared volatile-first; resolve() must reorder to static → step.
        specs = [
            {"source": {"step": "1"}},                                         # tier 1
            {"source": {"config": "meta_conversation", "output": "brief.md"}},  # tier 0
        ]
        result = resolver.resolve(specs, current_config="dpe_default")
        text = "\n".join(result.values())
        assert "Test project brief content" in text
        assert "SOTA Report" in text
        # static config read appears before the step output
        assert text.index("Test project brief content") < text.index("SOTA Report")
