"""Tests for skillflow.write_tools."""

from skillflow.write_tools import (generate_write_tool_schemas, resolve_write_target,
                                   execute_edit, execute_generic_create,
                                   execute_generic_edit)


class TestWriteTools:
    def test_content_mode_fixed_outputs(self):
        schemas = generate_write_tool_schemas(
            "content", {"sota": "step1_5_sota.md"}
        )
        # 3 tools per slot (write/create/edit) + finish_step
        assert len(schemas) == 4
        names = {s["name"] for s in schemas}
        assert names == {"write_sota", "create_sota", "edit_sota", "finish_step"}
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
        assert names == {"write_task_card", "create_task_card",
                         "edit_task_card", "finish_step"}
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
            "write_plan", "create_plan", "edit_plan",
            "write_manifest", "create_manifest", "edit_manifest",
            "finish_step",
        }

    def test_write_mode_no_fixed(self):
        """Generic write-mode exposes create + edit (NOT whole-file write) by default."""
        schemas = generate_write_tool_schemas("write", {})
        names = [s["name"] for s in schemas]
        # create + edit + finish_step — no 'write' (whole-file clobber) by default
        assert names == ["create", "edit", "finish_step"]
        assert "write" not in names
        create = next(s for s in schemas if s["name"] == "create")
        assert "file" in create["parameters"] and "content" in create["parameters"]
        edit = next(s for s in schemas if s["name"] == "edit")
        assert set(("file", "old_str", "new_str")).issubset(edit["parameters"])

    def test_write_mode_allow_full_write_opt_in(self):
        """allow_full_write=True additionally exposes the whole-file write primitive."""
        schemas = generate_write_tool_schemas("write", {}, allow_full_write=True)
        names = [s["name"] for s in schemas]
        assert names == ["create", "edit", "write", "finish_step"]
        write = next(s for s in schemas if s["name"] == "write")
        assert "file" in write["parameters"] and "content" in write["parameters"]

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

    # ── edit_{slot} executor ─────────────────────────────────────

    def test_execute_edit_replaces_in_existing_repo_file(self, tmp_path):
        """edit reads the existing repo file, str-replaces once, writes to staging."""
        repo = tmp_path / "repo"; repo.mkdir()
        staging = tmp_path / "stage"; staging.mkdir()
        (repo / "app.py").write_text("x = 1\ny = OLD\nz = 3\n")
        res = execute_edit("app", {"app": "app.py"},
                           {"old_str": "y = OLD", "new_str": "y = NEW"},
                           str(staging), source_dir=str(repo))
        assert res == {"edited": "app.py"}
        # Result lands in staging (for promotion); repo copy untouched until repo_apply
        assert (staging / "app.py").read_text() == "x = 1\ny = NEW\nz = 3\n"
        assert (repo / "app.py").read_text() == "x = 1\ny = OLD\nz = 3\n"

    def test_execute_edit_errors_when_not_unique(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        (repo / "app.py").write_text("dup\ndup\n")
        res = execute_edit("app", {"app": "app.py"},
                           {"old_str": "dup", "new_str": "x"},
                           str(tmp_path / "stage"), source_dir=str(repo))
        assert "error" in res and "2 times" in res["error"]

    def test_execute_edit_errors_when_missing_file(self, tmp_path):
        res = execute_edit("app", {"app": "app.py"},
                           {"old_str": "a", "new_str": "b"},
                           str(tmp_path / "stage"), source_dir=str(tmp_path / "repo"))
        assert "error" in res and "no existing version to edit" in res["error"]

    # ── generic create(file, content) executor ───────────────────

    def test_generic_create_writes_new_file_to_staging(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        stage = tmp_path / "stage"; stage.mkdir()
        res = execute_generic_create(
            {"file": "pkg/new_mod.py", "content": "X = 1\n"},
            str(stage), source_dir=str(repo))
        assert res == {"written": "pkg/new_mod.py"}
        assert (stage / "pkg/new_mod.py").read_text() == "X = 1\n"
        # repo untouched (only repo_apply mutates the repo)
        assert not (repo / "pkg/new_mod.py").exists()

    def test_generic_create_errors_if_exists_in_repo(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        (repo / "app.py").write_text("old\n")
        stage = tmp_path / "stage"; stage.mkdir()
        res = execute_generic_create(
            {"file": "app.py", "content": "new\n"},
            str(stage), source_dir=str(repo))
        assert "error" in res and "already exists" in res["error"]
        # nothing written — create can't clobber an existing file
        assert not (stage / "app.py").exists()

    def test_generic_create_errors_if_exists_in_staging(self, tmp_path):
        stage = tmp_path / "stage"; stage.mkdir()
        (stage / "app.py").write_text("first\n")
        res = execute_generic_create(
            {"file": "app.py", "content": "second\n"}, str(stage))
        assert "error" in res and "already exists" in res["error"]
        assert (stage / "app.py").read_text() == "first\n"

    # ── generic edit(file, old_str, new_str) executor ────────────

    def test_generic_edit_reads_repo_baseline_writes_staging(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        stage = tmp_path / "stage"; stage.mkdir()
        (repo / "core/db.py").parent.mkdir(parents=True)
        (repo / "core/db.py").write_text("def a(): ...\ndef b(): ...\n")
        res = execute_generic_edit(
            {"file": "core/db.py", "old_str": "def b(): ...",
             "new_str": "def b(): return 2"},
            str(stage), source_dir=str(repo))
        assert res == {"edited": "core/db.py"}
        # only the edited region changes; the rest carries through verbatim
        assert (stage / "core/db.py").read_text() == "def a(): ...\ndef b(): return 2\n"
        assert (repo / "core/db.py").read_text() == "def a(): ...\ndef b(): ...\n"

    def test_generic_edit_compounds_staging_first(self, tmp_path):
        """A 2nd edit to the same file reads the 1st edit's staged result, so
        edits to different regions compound instead of clobbering each other."""
        repo = tmp_path / "repo"; repo.mkdir()
        stage = tmp_path / "stage"; stage.mkdir()
        (repo / "m.py").write_text("A\nB\n")
        execute_generic_edit({"file": "m.py", "old_str": "A", "new_str": "A2"},
                             str(stage), source_dir=str(repo))
        execute_generic_edit({"file": "m.py", "old_str": "B", "new_str": "B2"},
                             str(stage), source_dir=str(repo))
        assert (stage / "m.py").read_text() == "A2\nB2\n"

    def test_generic_edit_errors_when_not_unique(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        (repo / "m.py").write_text("dup\ndup\n")
        res = execute_generic_edit({"file": "m.py", "old_str": "dup", "new_str": "x"},
                                   str(tmp_path / "stage"), source_dir=str(repo))
        assert "error" in res and "2 times" in res["error"]

    def test_generic_edit_errors_when_missing(self, tmp_path):
        res = execute_generic_edit({"file": "nope.py", "old_str": "a", "new_str": "b"},
                                   str(tmp_path / "stage"),
                                   source_dir=str(tmp_path / "repo"))
        assert "error" in res and "no existing version to edit" in res["error"]

    def test_generic_edit_rejects_path_traversal(self, tmp_path):
        res = execute_generic_edit({"file": "../escape.py", "old_str": "a", "new_str": "b"},
                                   str(tmp_path / "stage"), source_dir=str(tmp_path))
        assert "error" in res

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
        # Structured slot: the id argument IS the content's id field — the
        # coupling is structural now, not a prompt-hint plea.
        write_tool = next(t for t in schemas if t["name"] == "write_task_card")
        id_desc = write_tool["parameters"]["id"]["description"]
        assert "Replaces * in tasks/*.json" in id_desc
        assert "also written as the 'id' field" in id_desc

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


VERDICT_FIXED = {
    "verdict": {"file": "review_verdict.json", "on_exists": "new",
                "format": '{"passed": bool, "feedback": str, "suggestions": [str, ...]}'}
}


class TestStructuredJsonSlots:
    """Structured .json slots: the document rides inside the tool arguments,
    so JSON-in-a-string double-escaping (the review_verdict '\\.' incident)
    is structurally impossible — and a string fallback is validated."""

    # ── format-spec parser ──────────────────────────────────────

    def test_parse_format_verdict(self):
        from skillflow.write_tools import _parse_format_spec
        props = _parse_format_spec(
            '{"passed": bool, "feedback": str, "suggestions": [str, ...]}')
        assert props == {
            "passed": {"type": "boolean"},
            "feedback": {"type": "string"},
            "suggestions": {"type": "array", "items": {"type": "string"}},
        }

    def test_parse_format_nested_arrays(self):
        from skillflow.write_tools import _parse_format_spec
        props = _parse_format_spec('{"execution_order": [[str, ...], ...]}')
        assert props == {"execution_order": {
            "type": "array", "items": {"type": "array",
                                       "items": {"type": "string"}}}}

    def test_parse_format_prose_becomes_description(self):
        from skillflow.write_tools import _parse_format_spec
        props = _parse_format_spec(
            '{"id": str, "description": "ONE-LINE summary (max 80 chars)"}')
        assert props["description"] == {
            "type": "string",
            "description": "ONE-LINE summary (max 80 chars)"}

    def test_parse_format_named_shape_unparseable(self):
        from skillflow.write_tools import _parse_format_spec
        # References a named shape the mini-DSL can't resolve → None (tier 2)
        assert _parse_format_spec(
            '{"subtasks": [subtask, ...], "execution_order": [[str, ...], ...]}'
        ) is None

    def test_parse_format_non_object(self):
        from skillflow.write_tools import _parse_format_spec
        assert _parse_format_spec("free prose format") is None
        assert _parse_format_spec(None) is None
        assert _parse_format_spec("") is None

    # ── schema generation tiers ─────────────────────────────────

    def test_tier1_per_field_params(self):
        schemas = generate_write_tool_schemas("content", VERDICT_FIXED)
        create = next(s for s in schemas if s["name"] == "create_verdict")
        assert set(create["parameters"]) == {"passed", "feedback", "suggestions"}
        assert create["parameters"]["passed"] == {
            "type": "boolean", "required": True}
        assert create["parameters"]["suggestions"]["items"] == {"type": "string"}
        # no stringly content params anywhere
        assert "content" not in create["parameters"]
        assert "initialContent" not in create["parameters"]
        assert "do NOT pass the document as a JSON-encoded string" in create["description"]
        # edit stays textual (surgical string replace)
        edit = next(s for s in schemas if s["name"] == "edit_verdict")
        assert set(edit["parameters"]) == {"old_str", "new_str"}

    def test_tier2_object_content_when_format_unparseable(self):
        schemas = generate_write_tool_schemas("content", {
            "manifest": {"file": "subtasks_manifest.json",
                         "format": '{"subtasks": [subtask, ...]}'}
        })
        write = next(s for s in schemas if s["name"] == "write_manifest")
        assert write["parameters"]["content"]["type"] == "object"
        create = next(s for s in schemas if s["name"] == "create_manifest")
        assert create["parameters"]["initialContent"]["type"] == "object"

    def test_tier3_text_slot_unchanged(self):
        schemas = generate_write_tool_schemas("content", {
            "notes": {"file": "research_notes.md", "format": "prose format"}
        })
        write = next(s for s in schemas if s["name"] == "write_notes")
        assert write["parameters"]["content"] == {"type": "string",
                                                  "required": True}

    # ── executor: per-field assembly ────────────────────────────

    def test_create_assembles_document_from_fields(self, tmp_path):
        from skillflow.write_tools import execute_create
        import json as _json
        res = execute_create("verdict", VERDICT_FIXED, {
            "passed": True,
            "feedback": r"the `.replace(/\.0$/, '')` fix is correct",
            "suggestions": [],
        }, str(tmp_path))
        assert res == {"written": "review_verdict.json"}
        data = _json.loads((tmp_path / "review_verdict.json").read_text())
        # The backslash that used to kill the run survives intact
        assert data["passed"] is True
        assert "\\.0$" in data["feedback"]

    def test_create_missing_field_is_tool_error(self, tmp_path):
        from skillflow.write_tools import execute_create
        res = execute_create("verdict", VERDICT_FIXED,
                             {"passed": True}, str(tmp_path))
        assert "missing required field" in res["error"]
        assert not (tmp_path / "review_verdict.json").exists()

    def test_create_accepts_object_content(self, tmp_path):
        from skillflow.write_tools import execute_create
        import json as _json
        res = execute_create("verdict", VERDICT_FIXED, {
            "initialContent": {"passed": False, "feedback": "x",
                               "suggestions": ["y"]},
        }, str(tmp_path))
        assert res == {"written": "review_verdict.json"}
        data = _json.loads((tmp_path / "review_verdict.json").read_text())
        assert data["passed"] is False

    def test_create_validates_string_content(self, tmp_path):
        from skillflow.write_tools import execute_create
        # The exact incident: an under-escaped backslash in a JSON string
        bad = '{"passed": true, "feedback": "regex /\\.0$/ ok", "suggestions": []}'
        assert "\\." in bad  # single backslash-dot → invalid JSON escape
        res = execute_create("verdict", VERDICT_FIXED,
                             {"initialContent": bad}, str(tmp_path))
        assert "not valid JSON" in res["error"]
        assert not (tmp_path / "review_verdict.json").exists()

    def test_create_valid_string_still_accepted(self, tmp_path):
        from skillflow.write_tools import execute_create
        good = '{"passed": true, "feedback": "ok", "suggestions": []}'
        res = execute_create("verdict", VERDICT_FIXED,
                             {"initialContent": good}, str(tmp_path))
        assert res == {"written": "review_verdict.json"}

    def test_rejected_write_preserves_existing_file(self, tmp_path):
        from skillflow.write_tools import execute_create
        (tmp_path / "review_verdict.json").write_text('{"passed": false}')
        res = execute_create("verdict", VERDICT_FIXED,
                             {"initialContent": "{invalid"}, str(tmp_path))
        assert "error" in res
        # on_exists:new must NOT have archived the old file on a rejected write
        assert (tmp_path / "review_verdict.json").read_text() == '{"passed": false}'
        assert not (tmp_path / "review_verdict_1.json").exists()

    def test_glob_id_fills_filename_and_field(self, tmp_path):
        from skillflow.write_tools import execute_write
        import json as _json
        fixed = {"task_card": {"file": "tasks/*.json",
                               "format": '{"id": str, "description": str}'}}
        res = execute_write("task_card", fixed, {
            "id": "core_lib", "description": "build the core library",
        }, str(tmp_path))
        assert res == {"written": "tasks/core_lib.json"}
        data = _json.loads((tmp_path / "tasks/core_lib.json").read_text())
        assert data == {"id": "core_lib",
                        "description": "build the core library"}

    def test_text_slot_write_unchanged(self, tmp_path):
        from skillflow.write_tools import execute_write
        res = execute_write("notes", {"notes": "research_notes.md"},
                            {"content": "plain text with \\. backslash"},
                            str(tmp_path))
        assert res == {"written": "research_notes.md"}
        assert "\\." in (tmp_path / "research_notes.md").read_text()


class TestEditBaselineFallback:
    """Edit baseline for outputs that never reach the repo: the step's own
    promoted dir, passed as fallback_source_dir by the caller ONLY for a
    revision loop within the current run (cross-run gating is the caller's
    job — see core._edit_fallback_dir)."""

    def test_execute_edit_falls_back_to_prior_promoted_output(self, tmp_path):
        prior = tmp_path / "final"; prior.mkdir()
        (prior / "app.py").write_text("v2 fixed line\nrest\n")
        res = execute_edit("app", {"app": "app.py"},
                           {"old_str": "v2 fixed line", "new_str": "v3 line"},
                           str(tmp_path / "stage"),
                           source_dir=str(tmp_path / "repo"),
                           fallback_source_dir=str(prior))
        assert res == {"edited": "app.py"}
        assert (tmp_path / "stage" / "app.py").read_text() == "v3 line\nrest\n"
        # the baseline is read-only — the promoted dir is never edited in place
        assert (prior / "app.py").read_text() == "v2 fixed line\nrest\n"

    def test_execute_edit_staging_beats_fallback(self, tmp_path):
        # A file written earlier this same attempt is FRESHER than the
        # previous promoted version — staging wins.
        stage = tmp_path / "stage"; stage.mkdir()
        (stage / "app.py").write_text("staged this attempt\n")
        prior = tmp_path / "final"; prior.mkdir()
        (prior / "app.py").write_text("older promoted\n")
        res = execute_edit("app", {"app": "app.py"},
                           {"old_str": "staged this attempt", "new_str": "x"},
                           str(stage), source_dir=str(tmp_path / "repo"),
                           fallback_source_dir=str(prior))
        assert res == {"edited": "app.py"}
        assert (stage / "app.py").read_text() == "x\n"

    def test_execute_edit_no_fallback_still_errors(self, tmp_path):
        res = execute_edit("app", {"app": "app.py"},
                           {"old_str": "a", "new_str": "b"},
                           str(tmp_path / "stage"),
                           source_dir=str(tmp_path / "repo"),
                           fallback_source_dir="")
        assert "error" in res and "no existing version to edit" in res["error"]

    def test_generic_edit_falls_back_to_prior_promoted_output(self, tmp_path):
        prior = tmp_path / "final"; prior.mkdir()
        (prior / "notes.md").write_text("keep\nfix me\n")
        res = execute_generic_edit(
            {"file": "notes.md", "old_str": "fix me", "new_str": "fixed"},
            str(tmp_path / "stage"), source_dir="",
            fallback_source_dir=str(prior))
        assert res == {"edited": "notes.md"}
        assert (tmp_path / "stage" / "notes.md").read_text() == "keep\nfixed\n"
