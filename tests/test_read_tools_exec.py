# tests/test_read_tools_exec.py
# Execution-level tests for the UNIFIED read surface (read_tools.py):
# read / search / list over a step's working tree + declared sources, with
# staging-first working-tree reads, source tagging, an access gate, and
# deletion awareness.

import json

from skillflow.read_tools import (make_read_tool_fns, generate_read_tool_schemas,
                                  get_read_tool_names)


def _mk_step(tmp_path, files):
    """Create a dpe_default step-2 dir with the given {name: body} files."""
    step = tmp_path / "dpe_default" / "2"
    step.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        fp = step / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(body)
    return tmp_path


# A declared step-2 source, addressed as source="step:2".
_STEP_SPEC = {"source_type": "step", "step_id": "2"}


def _step_fns(tmp_path):
    return make_read_tool_fns([_STEP_SPEC], str(tmp_path), "dpe_default")


class TestReadPaging:
    def test_small_file_whole_not_truncated(self, tmp_path):
        _mk_step(tmp_path, {"a.txt": "L1\nL2\nL3"})
        r = _step_fns(tmp_path)["read"]("a.txt", source="step:2")
        assert r["total_lines"] == 3
        assert r["truncated"] is False
        assert "1\tL1" in r["content"]
        assert r["source"] == "step:2"

    def test_paging_window(self, tmp_path):
        _mk_step(tmp_path, {"a.txt": "\n".join(f"L{i}" for i in range(1, 51))})
        r = _step_fns(tmp_path)["read"]("a.txt", source="step:2",
                                        start_line=10, end_line=15)
        assert r["returned_lines"] == 5
        assert r["start_line"] == 10
        assert "11\tL11" in r["content"]

    def test_default_cap_flags_truncation(self, tmp_path):
        _mk_step(tmp_path, {"a.txt": "\n".join("x" for _ in range(3000))})
        r = _step_fns(tmp_path)["read"]("a.txt", source="step:2")
        assert r["returned_lines"] == 2000
        assert r["truncated"] is True

    def test_missing_file(self, tmp_path):
        _mk_step(tmp_path, {"a.txt": "x"})
        r = _step_fns(tmp_path)["read"]("nope.txt", source="step:2")
        assert "not found" in r["error"].lower()


class TestSearch:
    def test_basic_and_source_tag(self, tmp_path):
        _mk_step(tmp_path, {"a.py": "import os\ndef f():\n    pass\n"})
        r = _step_fns(tmp_path)["search"]("def ", source="step:2")
        assert len(r["matches"]) == 1
        assert r["matches"][0]["line"] == 2
        assert r["matches"][0]["source"] == "step:2"

    def test_glob_filter(self, tmp_path):
        _mk_step(tmp_path, {"a.py": "HIT\n", "b.md": "HIT\n"})
        r = _step_fns(tmp_path)["search"]("HIT", source="step:2", glob="*.py")
        assert {m["file"] for m in r["matches"]} == {"a.py"}

    def test_files_with_matches(self, tmp_path):
        _mk_step(tmp_path, {"a.py": "HIT\nHIT\n", "b.py": "no\n"})
        r = _step_fns(tmp_path)["search"]("HIT", source="step:2",
                                          files_with_matches=True)
        assert [f["file"] for f in r["files"]] == ["a.py"]

    def test_context_lines(self, tmp_path):
        _mk_step(tmp_path, {"a.py": "before\nMATCH\nafter\n"})
        r = _step_fns(tmp_path)["search"]("MATCH", source="step:2", context_lines=1)
        ctx = r["matches"][0]["context"]
        assert "before" in ctx and "after" in ctx

    def test_max_results_caps_and_flags(self, tmp_path):
        _mk_step(tmp_path, {"a.py": "\n".join("HIT" for _ in range(100))})
        r = _step_fns(tmp_path)["search"]("HIT", source="step:2", max_results=10)
        assert len(r["matches"]) == 10
        assert r["truncated"] is True

    def test_literal_fallback_on_bad_regex(self, tmp_path):
        _mk_step(tmp_path, {"a.py": "cost = price * (1 + tax)\n"})
        r = _step_fns(tmp_path)["search"]("(1 +", source="step:2")
        assert len(r["matches"]) == 1


class TestSchema:
    def test_three_tools_and_allowed_sources(self, tmp_path):
        _mk_step(tmp_path, {"a.py": "x"})
        schemas = generate_read_tool_schemas([_STEP_SPEC], str(tmp_path), "dpe_default")
        assert {s["name"] for s in schemas} == {"read", "search", "list"}
        read = next(s for s in schemas if s["name"] == "read")
        assert read["parameters"]["path"]["required"] is True
        assert "step:2" in read["parameters"]["source"]["description"]

    def test_get_names(self):
        assert get_read_tool_names([_STEP_SPEC]) == {"read", "search", "list"}
        assert get_read_tool_names([{"source_type": "step", "step_id": "2",
                                     "mode": "inline"}]) == set()

    def test_empty_when_nothing_readable(self, tmp_path):
        # step:2 doesn't exist and no repo/staging → nothing to read
        assert generate_read_tool_schemas([_STEP_SPEC], str(tmp_path), "dpe_default") == []
        assert make_read_tool_fns([_STEP_SPEC], str(tmp_path), "dpe_default") == {}

    def test_smap_only_call_shape(self, tmp_path):
        # The exact shape core.py uses: build the map once, pass _smap to both,
        # WITHOUT re-passing workspace_root. Regression: workspace_root was a
        # required positional, so this raised TypeError that core.py's
        # try/except swallowed → read tools silently never registered.
        _mk_step(tmp_path, {"a.py": "x = 1\n"})
        from skillflow.read_tools import build_source_map
        smap = build_source_map([_STEP_SPEC], str(tmp_path), "dpe_default")
        schemas = generate_read_tool_schemas([_STEP_SPEC], _smap=smap)
        fns = make_read_tool_fns([_STEP_SPEC], _smap=smap)
        assert {s["name"] for s in schemas} == {"read", "search", "list"}
        assert "x = 1" in fns["read"]("a.py", source="step:2")["content"]


class TestWorkingTreeStagingFirst:
    """Omitting `source` reads the working tree: own staging shadows the repo
    baseline so read-after-edit is consistent (regression for the
    subagent/t_impl 'read pristine → re-edit stale old_str → thrash' loop)."""

    def _setup(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        staging = tmp_path / "work.tmp"; staging.mkdir()
        (repo / "a.py").write_text("def old():\n    return 1\n")
        (repo / "untouched.py").write_text("x = 42\n")
        (staging / "a.py").write_text("def new():\n    return 2\n")   # edited
        (staging / "b.py").write_text("created = True\n")            # created
        return repo, staging

    def _fns(self, tmp_path):
        repo, staging = self._setup(tmp_path)
        return make_read_tool_fns([{"source_type": "repository", "mode": "tool"}],
                                  str(tmp_path), code_root=str(repo),
                                  step_tmp_dir=str(staging))

    def test_read_sees_staged_edit_tagged(self, tmp_path):
        r = self._fns(tmp_path)["read"]("a.py")
        assert "def new()" in r["content"] and "def old()" not in r["content"]
        assert r["source"] == "staging"

    def test_read_untouched_falls_through_to_repo(self, tmp_path):
        r = self._fns(tmp_path)["read"]("untouched.py")
        assert "x = 42" in r["content"] and r["source"] == "repo"

    def test_read_staging_only_created_file(self, tmp_path):
        assert "created = True" in self._fns(tmp_path)["read"]("b.py")["content"]

    def test_explicit_repo_source_is_pristine(self, tmp_path):
        r = self._fns(tmp_path)["read"]("a.py", source="repo")
        assert "def old()" in r["content"] and r["source"] == "repo"

    def test_list_dedups_staged_over_repo(self, tmp_path):
        files = {e["name"]: e["source"]
                 for e in json.loads(self._fns(tmp_path)["list"]())["files"]}
        assert files == {"a.py": "staging", "untouched.py": "repo",
                         "b.py": "staging"}

    def test_search_no_stale_repo_hit_no_dup(self, tmp_path):
        hits = self._fns(tmp_path)["search"]("def ")["matches"]
        assert any("def new" in h["text"] for h in hits)
        assert not any("def old" in h["text"] for h in hits)
        assert sum(1 for h in hits if h["file"] == "a.py") == 1
        assert all(h["source"] for h in hits)

    def test_no_overlay_reads_pristine_repo(self, tmp_path):
        repo, _ = self._setup(tmp_path)
        fns = make_read_tool_fns([{"source_type": "repository", "mode": "tool"}],
                                 str(tmp_path), code_root=str(repo))
        assert "def old()" in fns["read"]("a.py")["content"]


class TestAccessGate:
    def test_unknown_source_lists_allowed(self, tmp_path):
        _mk_step(tmp_path, {"a.py": "x"})
        r = _step_fns(tmp_path)["read"]("a.py", source="step:99")
        assert "unknown source" in r["error"]
        assert "step:2" in r["allowed_sources"]


class TestDeletionAwareness:
    def test_working_tree_reflects_queued_deletion(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        staging = tmp_path / "work.tmp"; staging.mkdir()
        (repo / "gone.py").write_text("still here\n")
        (staging / "_deletions.json").write_text(json.dumps(["gone.py"]))
        fns = make_read_tool_fns([{"source_type": "repository", "mode": "tool"}],
                                 str(tmp_path), code_root=str(repo),
                                 step_tmp_dir=str(staging))
        # working tree → deleted
        assert "deleted this step" in fns["read"]("gone.py")["error"]
        # explicit repo source still sees it (until repo_delete runs on deliver)
        assert "still here" in fns["read"]("gone.py", source="repo")["content"]


class TestSourceContainerRoot:
    """A declared step/config source is addressed against its container ROOT,
    so natural paths work and same-key file-specs don't collapse (Fix A)."""

    def test_file_spec_addresses_against_step_root(self, tmp_path):
        # PM step-3 output: task cards under tasks/
        step3 = tmp_path / "dpe_default" / "3" / "tasks"
        step3.mkdir(parents=True)
        (step3 / "frontend.json").write_text('{"id": "frontend"}')
        (step3 / "backend.json").write_text('{"id": "backend"}')
        spec = {"source_type": "step", "step_id": "3",
                "files": ["tasks/frontend.json"]}
        fns = make_read_tool_fns([spec], str(tmp_path), "dpe_default")
        # natural path relative to the step root resolves
        r = fns["read"]("tasks/frontend.json", source="step:3")
        assert '"id": "frontend"' in r["content"] and r["source"] == "step:3"
        # the declared unit's dir is listable (unit-level gating)
        names = {e["name"] for e in json.loads(fns["list"](source="step:3"))["files"]}
        assert names == {"tasks/frontend.json", "tasks/backend.json"}

    def test_stepless_config_output_scopes_to_its_step_dir(self, tmp_path):
        # Real subagent shape: {config: subagent, output: task.md} — no step_id,
        # scan-located. Must NOT widen to the whole config dir (other steps).
        (tmp_path / "subagent" / "gather").mkdir(parents=True)
        (tmp_path / "subagent" / "gather" / "task.md").write_text("THE TASK")
        (tmp_path / "subagent" / "work").mkdir(parents=True)
        (tmp_path / "subagent" / "work" / "secret.py").write_text("other step")
        spec = {"source_type": "config", "config_name": "subagent",
                "files": ["task.md"]}
        fns = make_read_tool_fns([spec], str(tmp_path), "subagent")
        # addressable by its own name, resolving in the step dir that holds it
        assert "THE TASK" in fns["read"]("task.md", source="config:subagent")["content"]
        # and does NOT leak sibling steps' files
        names = {e["name"] for e in
                 json.loads(fns["list"](source="config:subagent"))["files"]}
        assert names == {"task.md"}


class TestAllInlineNoReadTools:
    def test_inline_only_context_gets_no_read_surface(self, tmp_path):
        # A step whose only context is inline (e.g. a bare dir_tree tool) must
        # not be offered read/search/list — schema and allowlist must agree.
        repo = tmp_path / "repo"; repo.mkdir()
        (repo / "a.py").write_text("x")
        staging = tmp_path / "work.tmp"; staging.mkdir()
        specs = [{"source_type": "tool", "mode": "inline"}]
        assert get_read_tool_names(specs) == set()
        assert generate_read_tool_schemas(specs, str(tmp_path), code_root=str(repo),
                                          step_tmp_dir=str(staging)) == []
        assert make_read_tool_fns(specs, str(tmp_path), code_root=str(repo),
                                  step_tmp_dir=str(staging)) == {}


class TestSelfVsWorkingTree:
    """Working tree = staging → repo (matches edit's baseline); the promoted
    dir is reachable only via source='self' (Fix B)."""

    def test_promoted_only_file_absent_from_working_tree(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        staging = tmp_path / "work.tmp"; staging.mkdir()
        promoted = tmp_path / "work"; promoted.mkdir()
        (promoted / "prior.py").write_text("from a prior iteration\n")
        fns = make_read_tool_fns([{"source_type": "repository", "mode": "tool"}],
                                 str(tmp_path), code_root=str(repo),
                                 step_tmp_dir=str(staging), step_dir=str(promoted))
        # working tree (staging→repo) does NOT surface the promoted-only file
        assert "not found" in fns["read"]("prior.py")["error"].lower()
        # but source="self" (staging→promoted) does
        r = fns["read"]("prior.py", source="self")
        assert "prior iteration" in r["content"] and r["source"] == "promoted"


class TestStagingIncludedByPath:
    """Staging is included by path, not is_dir, so a read tool built BEFORE the
    agent's first write still sees files it writes afterward (Fix C)."""

    def test_staging_created_after_build(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        (repo / "a.py").write_text("orig\n")
        staging = tmp_path / "work.tmp"  # deliberately NOT created yet
        fns = make_read_tool_fns([{"source_type": "repository", "mode": "tool"}],
                                 str(tmp_path), code_root=str(repo),
                                 step_tmp_dir=str(staging))
        # agent's first edit lands now, after the map/closures were built
        staging.mkdir()
        (staging / "a.py").write_text("edited\n")
        r = fns["read"]("a.py")
        assert "edited" in r["content"] and r["source"] == "staging"


class TestForgivingResolution:
    """A wrong path must not be a dead end (live incident: the humanizer glued
    a repo-ledger path onto a step source, got a bare 'File not found', probed
    the wrong source once, gave up). Deterministic recovery only."""

    def test_unique_basename_resolves_with_corrected_path(self, tmp_path):
        _mk_step(tmp_path, {"chapter_draft.md": "正文全文"})
        r = _step_fns(tmp_path)["read"](
            "novel/chapters/ch0003/chapter_draft.md", source="step:2")
        assert "error" not in r
        assert "正文全文" in r["content"]
        assert r["path"] == "chapter_draft.md"                       # corrected
        assert r["resolved_from"] == "novel/chapters/ch0003/chapter_draft.md"

    def test_unique_basename_resolves_in_subdir_too(self, tmp_path):
        _mk_step(tmp_path, {"docs/spec.md": "深埋的文件"})
        r = _step_fns(tmp_path)["read"]("spec.md", source="step:2")
        assert "error" not in r and r["path"] == "docs/spec.md"

    def test_ambiguous_basename_lists_candidates(self, tmp_path):
        _mk_step(tmp_path, {"a/report.md": "A", "b/report.md": "B"})
        r = _step_fns(tmp_path)["read"]("wrong/report.md", source="step:2")
        assert "error" in r
        cands = {c["path"] for c in r["candidates"]}
        assert cands == {"a/report.md", "b/report.md"}
        assert "exact paths" in r["hint"]

    def test_shadowed_same_relpath_is_not_ambiguity(self, tmp_path):
        # working tree = staging over repo; the SAME rel path in both layers is
        # shadowing (higher layer wins), not ambiguity — must still resolve.
        stage = tmp_path / "stage"; stage.mkdir()
        (stage / "note.md").write_text("staged")
        repo = tmp_path / "repo"; repo.mkdir()
        (repo / "note.md").write_text("repo")
        fns = make_read_tool_fns([{"source_type": "repository", "mode": "tool"}],
                                 str(tmp_path), "dpe_default",
                                 code_root=str(repo), step_tmp_dir=str(stage))
        r = fns["read"]("somewhere/else/note.md")
        assert "error" not in r
        assert r["content"].endswith("staged") or "staged" in r["content"]
        assert r["resolved_from"] == "somewhere/else/note.md"

    def test_no_match_lists_available_files(self, tmp_path):
        _mk_step(tmp_path, {"a.txt": "x", "b.txt": "y"})
        r = _step_fns(tmp_path)["read"]("ghost.md", source="step:2")
        assert "error" in r
        avail = {f for entry in r["available"] for f in entry["files"]}
        assert {"a.txt", "b.txt"} <= avail
