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

    def test_mode_applies_without_a_named_file(self, workspace):
        """`{step: N, mode: ...}` with no `file:` must still project.

        The regression this pins: mode was read only on the named-single-file
        branch, so the bare form silently returned the whole step directory.
        A config asking for an 8 KB interfaces slice got a 39 KB document under
        a label that said "interfaces", and nothing anywhere said the
        projection had not happened.
        """
        resolver = ContextResolver(workspace)
        full = list(resolver.resolve(
            [{"source": {"step": "2"}}],
            current_config="dpe_default").values())[0]
        sliced = list(resolver.resolve(
            [{"source": {"step": "2", "mode": "interfaces"}}],
            current_config="dpe_default").values())[0]
        # Asserted on CONTENT, not length: on a small document the truncation
        # marker is longer than what it replaced, so a byte-count test calls a
        # working projection broken.
        assert "GET /api" in full and "Extra info" in full
        assert "GET /api" in sliced
        assert "Extra info" not in sliced

    def test_projected_directory_still_lists_every_file(self, workspace):
        """A projection may shrink a file; it may not hide one.

        Naming a single file is the other way to trim a directory, and it
        erases the siblings instead of truncating them — the reader cannot ask
        for what it cannot see. So a projected directory announces its whole
        contents.
        """
        resolver = ContextResolver(workspace)
        content = list(resolver.resolve(
            [{"source": {"step": "2", "mode": "interfaces"}}],
            current_config="dpe_default").values())[0]
        assert "[step output contains]" in content
        assert "step2_design.md" in content

    def test_unprojected_directory_is_byte_identical(self, workspace):
        """No mode → no header. This block feeds provider prefix caches, so a
        source that asked for nothing must be unchanged to the byte."""
        resolver = ContextResolver(workspace)
        content = list(resolver.resolve([{"source": {"step": "2"}}],
                                        current_config="dpe_default").values())[0]
        assert "[step output contains]" not in content

    def test_bare_and_named_share_one_label(self, workspace):
        """The label must not change when a mode is applied.

        A host may drop a context entry by exact label (AItelier hoists step
        1/2 into a cached system preamble and removes `"Step 2"` from the user
        message). Projecting must not rename the source out from under that
        drop — doing so puts the same document in the prompt twice.
        """
        resolver = ContextResolver(workspace)
        plain = resolver.resolve([{"source": {"step": "2"}}],
                                 current_config="dpe_default")
        sliced = resolver.resolve([{"source": {"step": "2",
                                               "mode": "interfaces"}}],
                                  current_config="dpe_default")
        assert list(plain.keys()) == list(sliced.keys()) == ["Step 2"]

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


class TestFeedbackOfSource:
    """{feedback_of: "step"} — inject another step's accumulated checkpoint-
    feedback log (e.g. onto a reviewer, so a revision that silently reverts an
    earlier round's fix no longer passes review unchallenged)."""

    def test_resolves_log_with_read_contract(self, workspace):
        fb = workspace / "dpe_default" / "_feedback"
        fb.mkdir(parents=True)
        (fb / "2.md").write_text("## 反馈轮 #1 · ts\n\n别用真实地名\n",
                                 encoding="utf-8")
        resolver = ContextResolver(workspace)
        result = resolver.resolve([{"feedback_of": "2"}],
                                  current_config="dpe_default")
        assert len(result) == 1
        label = next(iter(result))
        assert "feedback on step '2'" in label
        content = result[label]
        assert "别用真实地名" in content
        # the read contract rides along: quotes locate problems, they are not
        # text to reproduce; feedback constrains the artifact rather than
        # belonging in it; every round stays binding
        assert "How to read this feedback log" in content
        assert "NOT text to reproduce" in content
        assert "CONSTRAINT on the artifact" in content

    def test_absent_log_resolves_to_nothing(self, workspace):
        resolver = ContextResolver(workspace)
        result = resolver.resolve([{"feedback_of": "2"}],
                                  current_config="dpe_default")
        assert result == {}

    def test_feedback_is_volatile_ordered_after_step_outputs(self, workspace):
        fb = workspace / "dpe_default" / "_feedback"
        fb.mkdir(parents=True)
        (fb / "2.md").write_text("轮次内容", encoding="utf-8")
        resolver = ContextResolver(workspace)
        specs = [
            {"feedback_of": "2"},  # declared FIRST on purpose
            {"source": {"step": "2", "output": "step2_design.md"}},
        ]
        result = resolver.resolve(specs, current_config="dpe_default")
        labels = list(result)
        assert len(labels) == 2
        # feedback changes every reject round — it must sort to the volatile
        # tail so it can't poison the prompt-cache prefix
        assert "feedback" in labels[-1].lower()


class TestRepositoryRootConsistency:
    """`from: repository` must mean the CODE repo in every mode. The inline
    branch used to read workspace_root/"project" (a near-empty brief dir)
    while the read-tool branch of the SAME spec used the real code repo."""

    def test_inline_with_path_reads_code_root(self, workspace, tmp_path):
        code = tmp_path / "repo"
        (code / "novel").mkdir(parents=True)
        (code / "novel" / "state.md").write_text("初始状态", encoding="utf-8")
        # decoy at the OLD (wrong) root — must NOT be read
        decoy = workspace / "project" / "novel"
        decoy.mkdir(parents=True)
        (decoy / "state.md").write_text("WRONG ROOT", encoding="utf-8")

        from skillflow.graph import _normalize_context_spec
        resolver = ContextResolver(workspace, code_root=code)
        result = resolver.resolve(
            [_normalize_context_spec({"from": "repository", "path": "novel/state.md"})],
            current_config="dpe_default")
        assert len(result) == 1
        content = list(result.values())[0]
        assert "初始状态" in content and "WRONG ROOT" not in content

    def test_inline_without_path_refuses_whole_repo_dump(self, workspace, tmp_path):
        # a populated repo that would previously be concatenated wholesale
        code = tmp_path / "repo"
        code.mkdir()
        for i in range(3):
            (code / f"f{i}.py").write_text("x" * 1000)
        from skillflow.graph import _normalize_context_spec
        resolver = ContextResolver(workspace, code_root=code)
        result = resolver.resolve([_normalize_context_spec({"from": "repository"})],
                                  current_config="dpe_default")
        assert result == {}  # refused, not a 3KB (or 4MB) paste

    def test_mode_tool_still_injects_nothing(self, workspace, tmp_path):
        code = tmp_path / "repo"
        code.mkdir()
        (code / "a.md").write_text("data")
        from skillflow.graph import _normalize_context_spec
        resolver = ContextResolver(workspace, code_root=code)
        result = resolver.resolve(
            [_normalize_context_spec({"from": "repository", "mode": "tool"})],
            current_config="dpe_default")
        assert result == {}

    def test_default_code_root_preserves_legacy_path(self, workspace):
        # constructed WITHOUT code_root → old behavior (workspace/"project")
        legacy = workspace / "project"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "brief.md").write_text("legacy brief", encoding="utf-8")
        from skillflow.graph import _normalize_context_spec
        resolver = ContextResolver(workspace)
        result = resolver.resolve(
            [_normalize_context_spec({"from": "repository", "path": "brief.md"})],
            current_config="dpe_default")
        assert "legacy brief" in (list(result.values()) or [""])[0]


class TestInterfacesTruncationIsAnnounced:
    """An `interfaces` extract must never look like the whole document.

    It drops content twice — non-matching sections, then a byte cap — and used
    to return `extracted[:8000]` bare. On a real 41 KB design doc that is an
    81% cut with nothing marking it, so a reviewer handed the result reviews
    against a fifth of the design believing it has all of it. `summary` mode
    has always emitted "... [summary truncated]"; these bind the same promise
    here, including the pointer to the read tools that can fetch the rest.
    """

    @staticmethod
    def _doc(interface_body_chars: int, tail_chars: int = 0) -> str:
        body = "\n".join(f"- GET /api/{i}" for i in range(interface_body_chars // 14))
        doc = f"# Architecture\n## Interface\n{body}\n"
        if tail_chars:
            doc += "## Notes\n" + ("x" * tail_chars) + "\n"
        return doc

    def test_byte_cap_is_announced_and_names_the_file(self):
        from skillflow.context import ContextResolver, _INTERFACES_MAX_CHARS
        out = ContextResolver._extract_interfaces(
            self._doc(_INTERFACES_MAX_CHARS * 3), "step2_design.md")
        assert "TRUNCATED" in out
        assert "step2_design.md" in out          # what to open
        assert "read/search" in out              # how to open it
        assert str(_INTERFACES_MAX_CHARS) in out  # how much was shown

    def test_dropped_sections_are_announced_even_under_the_cap(self):
        from skillflow.context import ContextResolver
        out = ContextResolver._extract_interfaces(
            self._doc(200, tail_chars=5000), "step2_design.md")
        assert "GET /api/0" in out
        assert "xxxx" not in out                 # non-interface section dropped…
        assert "INTERFACES EXTRACT" in out       # …and said so
        assert "step2_design.md" in out

    def test_no_marker_when_nothing_was_dropped(self):
        """A trailing-newline delta is not a truncation.

        The extractor rebuilds with "\n".join(), losing the source's final
        newline — so a naive length test marked a document that dropped
        NOTHING as truncated. A marker that fires on complete documents is
        one the reader learns to skip past on the cut that matters.
        """
        from skillflow.context import ContextResolver
        whole = "## Interface\n- GET /api\n"
        assert "\u26a0" not in ContextResolver._extract_interfaces(whole, "d.md")

    def test_keyword_fallback_announces_the_line_cut(self):
        from skillflow.context import ContextResolver
        text = "# Misc\n" + "\n".join(f"line {i}" for i in range(400))
        out = ContextResolver._extract_interfaces(text, "step2_design.md")
        assert "NO INTERFACE SECTIONS FOUND" in out
        assert "step2_design.md" in out
        assert "read/search" in out

    def test_marker_survives_the_real_resolve_path(self, workspace):
        """The caller must pass the filename through — not just the helper."""
        from skillflow.context import ContextResolver, _INTERFACES_MAX_CHARS
        big = workspace / "dpe_default" / "2" / "big_design.md"
        big.write_text(self._doc(_INTERFACES_MAX_CHARS * 3))
        resolver = ContextResolver(workspace)
        result = resolver.resolve(
            [{"source": {"step": "2", "output": "big_design.md",
                         "mode": "interfaces"}}],
            current_config="dpe_default")
        content = list(result.values())[0]
        assert "TRUNCATED" in content
        assert "big_design.md" in content



class TestADirectorySourceIsBounded:
    """A step's output directory is not all prose, and not all small.

    One Godot play-test step writes 184 PNG frames (101 MB). Decoded with
    `errors="replace"` they became 91 MB of replacement characters inside ONE
    step's persisted inputs — a single database row bigger than most databases,
    which no reader could use and no prompt could hold. The live deployment had
    15 such rows totalling 1,008 MB in a 2 GB file.
    """

    def test_binaries_are_skipped_and_named(self, workspace):
        step = workspace / "dpe_default" / "2"
        (step / "frames").mkdir(parents=True, exist_ok=True)
        (step / "frames" / "f0.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\xff" * 5000)
        (step / "notes.md").write_text("real content")
        content = list(ContextResolver(workspace).resolve(
            [{"source": {"step": "2"}}], current_config="dpe_default").values())[0]
        assert "real content" in content
        assert "frames/f0.png" in content, "a skipped file must still be named"
        assert "�" not in content, "binary was decoded into the context"

    def test_an_extensionless_binary_is_sniffed(self, workspace):
        step = workspace / "dpe_default" / "2"
        step.mkdir(parents=True, exist_ok=True)
        (step / "blob").write_bytes(b"\x00\x01\x02" * 4000)
        content = list(ContextResolver(workspace).resolve(
            [{"source": {"step": "2"}}], current_config="dpe_default").values())[0]
        assert "�" not in content
        assert "blob" in content

    def test_a_giant_text_directory_is_cut_with_a_marker(self, workspace):
        step = workspace / "dpe_default" / "2"
        step.mkdir(parents=True, exist_ok=True)
        (step / "huge.log").write_text("x" * 3_000_000)
        content = list(ContextResolver(workspace).resolve(
            [{"source": {"step": "2"}}], current_config="dpe_default").values())[0]
        assert len(content) < 2_100_000
        assert "TRUNCATED" in content
