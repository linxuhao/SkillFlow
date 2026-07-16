"""Constrained write tool generation from output.fixed config.

Given a step's output config:

    output_mode: "content"
    output_fixed:
      sota: "step1_sota.md"
      verdict: { file: "review_verdict.json", on_exists: "new" }

Generates write, create, and edit tool variants per slot:

- write_{slot}(content) — replace file entirely
- create_{slot}(initialContent) — write to canonical filename; if file exists,
  archive old file with numeric suffix, so the canonical name always holds the
  latest version
- edit_{slot}(old_str, new_str) — surgical in-place replace of an exact unique
  snippet in the existing file

For output_mode="write" with no fixed outputs: write(file, content).

on_exists in the config still controls the default write behaviour.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


def _ensure_str(value, default: str = "") -> str:
    """Coerce value to string for write_text. LLM native tool calling may
    pass structured objects (dict) for parameters typed as 'string'."""
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return json.dumps(value, ensure_ascii=False)


# ── Structured JSON slots ────────────────────────────────────────────
# A model asked to author a JSON document as a STRING inside tool-call JSON
# must escape every backslash twice; under-escaping by one level (e.g. a
# regex `/\.0$/` quoted in review feedback) produces a file that json.loads
# rejects — and the run dies on an unmatched transition. For .json slots we
# therefore move the document INTO the tool arguments, where the provider's
# constrained decoding guarantees validity:
#   tier 1 — the slot's `format` spec parses → one argument per field;
#   tier 2 — no parseable format → a single `content` argument typed OBJECT;
#   tier 3 — a string is still accepted but json.loads-validated at write
#            time, returning an actionable tool error instead of writing a
#            file no reader can parse.

_TYPE_TOKENS = {
    "bool": {"type": "boolean"},
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "num": {"type": "number"},
}


def _is_json_file(pattern: str) -> bool:
    return pattern.lower().endswith(".json")


def _format_value_to_spec(value) -> dict | None:
    """Convert one parsed format value into a parameter spec, or None."""
    if isinstance(value, str):
        token = value[2:-2] if value.startswith("__") and value.endswith("__") else None
        if token in _TYPE_TOKENS:
            return dict(_TYPE_TOKENS[token])
        # Free-text value = a string field described by that text
        # (e.g. "description": "ONE-LINE summary (max 80 chars)").
        return {"type": "string", "description": value}
    if isinstance(value, list):
        if len(value) != 1:
            return None
        items = _format_value_to_spec(value[0])
        if items is None:
            return None
        return {"type": "array", "items": items}
    if isinstance(value, dict):
        props = {}
        for k, v in value.items():
            spec = _format_value_to_spec(v)
            if spec is None:
                return None
            props[k] = spec
        return {"type": "object", "properties": props}
    return None


def _parse_format_spec(format_spec) -> dict | None:
    """Parse a pseudo-JSON ``format`` spec into per-field parameter specs.

    ``'{"passed": bool, "feedback": str, "suggestions": [str, ...]}'``
    → ``{"passed": {"type": "boolean"}, "feedback": {"type": "string"},
        "suggestions": {"type": "array", "items": {"type": "string"}}}``

    Returns None when the spec is absent or uses constructs the mini-DSL
    can't resolve (e.g. a named shape like ``[subtask, ...]``) — callers
    then fall back to a whole-document object parameter.
    """
    if not format_spec or not isinstance(format_spec, str):
        return None
    spec = format_spec.strip()
    if not spec.startswith("{"):
        return None
    # ", ..." ellipses are illustrative — drop them
    normalized = re.sub(r",\s*\.\.\.", "", spec)
    # quote bare type tokens so the spec becomes valid JSON
    normalized = re.sub(
        r"\b(bool|str|int|float|num)\b", r'"__\1__"', normalized)
    try:
        data = json.loads(normalized)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    props = {}
    for key, value in data.items():
        spec_v = _format_value_to_spec(value)
        if spec_v is None:
            return None
        props[key] = spec_v
    return props


def _normalize_fixed_entry(value) -> dict:
    """Normalize a fixed output entry to {file, on_exists, format} dict."""
    if isinstance(value, str):
        return {"file": value, "on_exists": "replace"}
    return {
        "file": value.get("file", value.get("output", "")),
        "on_exists": value.get("on_exists", "replace"),
        "format": value.get("format"),
    }


def _get_pattern(slot: str, fixed: dict) -> str:
    """Extract the file pattern string from fixed[slot]."""
    entry = fixed.get(slot)
    if entry is None:
        return ""
    if isinstance(entry, str):
        return entry
    return entry.get("file", entry.get("output", ""))


def _get_on_exists(slot: str, fixed: dict) -> str:
    """Extract on_exists mode from fixed[slot]."""
    entry = fixed.get(slot)
    if entry is None or isinstance(entry, str):
        return "replace"
    return entry.get("on_exists", "replace")


def _archive_old_file(directory: Path, base_name: str) -> str | None:
    """If base_name exists, rename it to {stem}_{i}{suffix}.

    Returns the archive filename, or None if no file existed.
    """
    candidate = directory / base_name
    if not candidate.exists():
        return None
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix
    i = 1
    while True:
        name = f"{stem}_{i}{suffix}"
        if not (directory / name).exists():
            os.rename(str(candidate), str(directory / name))
            return name
        i += 1


def generate_write_tool_schemas(output_mode: str,
                                fixed: dict,
                                allow_full_write: bool = False) -> list[dict]:
    """Generate tool schema dicts for write/create/edit/append tools.

    Returns a list of dicts with 'name', 'description', 'parameters'.

    For generic write-mode (``mode: write`` with no fixed slots) the default
    tools are ``create`` (new files) + ``edit`` (surgical in-place change) —
    NOT whole-file ``write``. Rewriting a whole existing file from the model's
    (necessarily partial) view silently drops any region it didn't reproduce;
    ``edit`` carries the rest of the file through verbatim, so that failure mode
    is impossible. Whole-file ``write`` is exposed only when ``allow_full_write``
    is set on the step (rare: genuine from-scratch authorship).
    """
    if output_mode == "write" and not fixed:
        tools = [{
            "name": "create",
            "description": (
                "Create a NEW file with the given content. The 'file' param is a "
                "repo-relative path (e.g. 'core/db_manager.py'). Fails if the file "
                "already exists — use 'edit' to change an existing file."
            ),
            "parameters": {
                "file": {"type": "string", "required": True},
                "content": {"type": "string", "required": True},
            },
        }, {
            "name": "edit",
            "description": (
                "Surgically change an EXISTING file by replacing an exact, unique "
                "snippet — use this to fix or update part of a file without "
                "rewriting the whole thing (the rest is preserved verbatim). "
                "'old_str' must appear exactly once; include surrounding context "
                "to make it unique. Fails if the file is absent or 'old_str' isn't "
                "found exactly once. For multiple changes, call edit repeatedly."
            ),
            "parameters": {
                "file": {"type": "string", "required": True},
                "old_str": {"type": "string", "required": True,
                            "description": "Exact text to find (must appear exactly once)."},
                "new_str": {"type": "string", "required": True,
                            "description": "Replacement text."},
            },
        }]
        if allow_full_write:
            tools.append({
                "name": "write",
                "description": ("Write a whole file, replacing it entirely if it "
                                "exists. Prefer 'edit' for existing files."),
                "parameters": {
                    "file": {"type": "string", "required": True},
                    "content": {"type": "string", "required": True},
                },
            })
        tools.append({
            "name": "finish_step",
            "description": (
                "Signal that all required output files have been written and "
                "the step is complete. Call this ONLY after all create/edit/write "
                "tool calls in the current turn have been made — it must be the "
                "last call."
            ),
            "parameters": {
                "summary": {"type": "string", "required": False,
                           "description": "Brief summary of what was created or completed"},
            },
        })
        return tools

    if output_mode == "content":
        tools = []
        for slot, entry in fixed.items():
            normalized = _normalize_fixed_entry(entry)
            pattern = normalized["file"]
            format_spec = normalized.get("format")
            fmt_hint = f"\nExpected format: {format_spec.strip()}" if format_spec else ""
            is_glob = "*" in pattern

            field_props = (_parse_format_spec(format_spec)
                           if _is_json_file(pattern) else None)

            if field_props:
                # Tier 1 — one argument per document field: the model authors
                # no JSON-in-a-string, so backslashes in field text can never
                # produce an unparseable file.
                params = {}
                if is_glob and "id" not in field_props:
                    params["id"] = {"type": "string", "required": True,
                                    "description": f"Replaces * in {pattern}"}
                for fname, fspec in field_props.items():
                    p = dict(fspec)
                    p["required"] = True
                    if is_glob and fname == "id":
                        extra = f"Replaces * in {pattern}; also written as the 'id' field."
                        p["description"] = (
                            f"{p.get('description', '')} {extra}".strip())
                    params[fname] = p
                create_params = dict(params)
                write_hint = (fmt_hint + "\nProvide one argument per field — "
                              "do NOT pass the document as a JSON-encoded string.")
            elif _is_json_file(pattern):
                # Tier 2 — whole document as a structured OBJECT argument
                # (format absent or beyond the mini-DSL).
                content_spec = {
                    "type": "object", "required": True,
                    "description": ("The complete JSON document as a structured "
                                    "object — NOT a JSON-encoded string."),
                }
                if is_glob:
                    id_desc = f"Replaces * in {pattern}"
                    if format_spec and '"id"' in format_spec:
                        id_desc += " — must equal the 'id' field value in your document"
                    params = {
                        "id": {"type": "string", "required": True,
                               "description": id_desc},
                        "content": content_spec,
                    }
                else:
                    params = {"content": content_spec}
                create_params = dict(params)
                create_params["initialContent"] = create_params.pop("content")
                write_hint = fmt_hint
            else:
                # Text slots (md etc.) — plain string content, unchanged.
                if is_glob:
                    id_desc = f"Replaces * in {pattern}"
                    if format_spec and '"id"' in format_spec:
                        id_desc += " — must equal the 'id' field value in your file content"
                    params = {
                        "id": {"type": "string", "required": True,
                               "description": id_desc},
                        "content": {"type": "string", "required": True},
                    }
                else:
                    params = {"content": {"type": "string", "required": True}}
                create_params = dict(params)
                create_params["initialContent"] = create_params.pop("content")
                write_hint = fmt_hint

            # write_{slot} — full replace
            tools.append({
                "name": f"write_{slot}",
                "description": f"Replace {pattern} with new content.{write_hint}",
                "parameters": params,
            })

            # create_{slot} — create or archive-and-create (flipped rename)
            tools.append({
                "name": f"create_{slot}",
                "description": (
                    f"Create {pattern} with initial content. "
                    f"If file already exists, it is archived with a numeric suffix, "
                    f"so {pattern} always holds the latest version.{write_hint}"
                ),
                "parameters": create_params,
            })

            # edit_{slot} — surgical in-place edit of the EXISTING file
            edit_params = {
                "old_str": {"type": "string", "required": True,
                            "description": "Exact text to find (must appear exactly once)."},
                "new_str": {"type": "string", "required": True,
                            "description": "Replacement text."},
            }
            if is_glob:
                edit_params = {"id": dict(params["id"]), **edit_params}
            tools.append({
                "name": f"edit_{slot}",
                "description": (
                    f"Surgically edit the EXISTING {pattern} by replacing an exact "
                    f"unique snippet — use this to fix or update part of a file "
                    f"without rewriting the whole thing (preserves the rest). "
                    f"PREFERRED on a revision round (reviewer/user feedback on a "
                    f"previous version): edit exactly the flagged spots — a full "
                    f"rewrite from memory silently corrupts unflagged parts. "
                    f"Fails if the file is absent or old_str isn't found exactly once.{fmt_hint}"
                ),
                "parameters": edit_params,
            })
        # finish_step — signal completion (always last so the model calls it last)
        tools.append({
            "name": "finish_step",
            "description": (
                "Signal that all required output files have been written and "
                "the step is complete. Call this ONLY after all write/create/append"
                " tool calls in the current turn have been made — it must be the "
                "last tool call in your response."
            ),
            "parameters": {
                "summary": {"type": "string", "required": False,
                           "description": "Brief summary of what was created or completed"},
            },
        })
        return tools

    return []


def _resolve_slot_document(slot: str, fixed: dict, params: dict) -> "tuple[str | None, dict | None]":
    """Resolve the text to write for a write_/create_{slot} call.

    Returns ``(text, None)`` on success or ``(None, error_dict)`` — the error
    goes back to the agent as the tool result, so it can self-correct within
    the same step instead of a downstream reader choking on a bad file.

    JSON slots accept three input shapes: per-field arguments (tier 1),
    a structured object in content/initialContent (tier 2), or a raw string
    that must survive ``json.loads`` (tier 3 — the legacy path that used to
    write invalid escapes straight to disk). Non-JSON slots keep the legacy
    string behaviour verbatim.
    """
    entry = fixed.get(slot)
    normalized = _normalize_fixed_entry(entry) if entry is not None else {"file": ""}
    pattern = normalized.get("file", "")

    raw = params.get("content")
    if raw is None or raw == "":
        raw = params.get("initialContent")

    if not _is_json_file(pattern):
        return _ensure_str(raw, ""), None

    field_props = _parse_format_spec(normalized.get("format"))
    if field_props and raw is None:
        doc, missing = {}, []
        for fname in field_props:
            if fname in params:
                doc[fname] = params[fname]
            else:
                missing.append(fname)
        if missing:
            return None, {"error": (
                f"{slot}: missing required field argument(s): "
                f"{', '.join(missing)} — provide one argument per field "
                f"of the document.")}
        return json.dumps(doc, indent=2, ensure_ascii=False), None

    if isinstance(raw, (dict, list)):
        return json.dumps(raw, indent=2, ensure_ascii=False), None

    if isinstance(raw, str) and raw.strip():
        try:
            json.loads(raw)
        except json.JSONDecodeError as e:
            return None, {"error": (
                f"content for '{pattern}' is not valid JSON: {e}. Pass the "
                f"document as structured arguments (or a plain object), not a "
                f"JSON-encoded string; if you must pass a string, double every "
                f"backslash (\\\\).")}
        return raw, None

    return None, {"error": f"{slot}: no content provided"}


def resolve_write_target(slot: str, fixed: dict, params: dict) -> str:
    """Resolve the actual filename to write for a given write_* call."""
    entry = fixed.get(slot)
    if entry is None:
        return params.get("file", "")
    pattern = _get_pattern(slot, fixed)
    if "*" in pattern:
        return pattern.replace("*", params.get("id", "unknown"))
    return pattern


def execute_write(slot: str, fixed: dict, params: dict,
                  output_dir: str, on_exists: str = "replace") -> dict:
    """Execute a write_{slot} tool call. Resolves filename, applies on_exists mode."""
    base_name = resolve_write_target(slot, fixed, params)
    mode = on_exists if on_exists != "replace" else _get_on_exists(slot, fixed)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    if mode == "append":
        filename = base_name
        path = directory / filename
        content = _ensure_str(params.get("content", ""))
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(existing + content, encoding="utf-8")
        return {"written": filename}

    # Resolve (and validate) the document BEFORE any archive rename, so a
    # rejected write leaves the existing canonical file untouched.
    content, err = _resolve_slot_document(slot, fixed, params)
    if err:
        return err

    archived = None
    if mode == "new":
        # Flipped rename: archive old file, write new content to canonical name
        archived = _archive_old_file(directory, base_name)
    filename = base_name
    path = directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result = {"written": filename}
    if mode == "new" and archived:
        result["archived"] = archived
    return result


def execute_create(slot: str, fixed: dict, params: dict,
                   output_dir: str) -> dict:
    """Execute a create_{slot} call: archive old, write new to canonical name."""
    base_name = resolve_write_target(slot, fixed, params)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # Resolve (and validate) BEFORE archiving so a rejected write leaves the
    # existing canonical file untouched.
    content, err = _resolve_slot_document(slot, fixed, params)
    if err:
        return err
    archived = _archive_old_file(directory, base_name)
    path = directory / base_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result = {"written": base_name}
    if archived:
        result["archived"] = archived
    return result


def execute_append(slot: str, fixed: dict, params: dict,
                   output_dir: str) -> dict:
    """Execute an append_{slot} call: append to the canonical filename."""
    base_name = resolve_write_target(slot, fixed, params)
    path = Path(output_dir) / base_name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _ensure_str(params.get("content", ""))
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing + content, encoding="utf-8")
    return {"written": base_name}


def _unique_replace(content: str, old_str: str, new_str: str, *,
                    tool: str, name: str) -> "tuple[str | None, dict | None]":
    """Replace the single exact occurrence of ``old_str`` in ``content``.

    Shared by the slot-mode (``edit_{slot}``) and generic (``edit``) executors so
    the uniqueness-and-replace rule — the safety-critical heart of a surgical
    edit — lives in exactly one place. Returns ``(updated_content, None)`` on
    success, or ``(None, error_dict)`` when ``old_str`` is absent or not unique.
    ``tool`` and ``name`` only shape the error message (caller's tool label and
    the file path).
    """
    occurrences = content.count(old_str)
    if occurrences == 0:
        return None, {"error": f"{tool}: 'old_str' not found in '{name}'"}
    if occurrences > 1:
        return None, {"error": (f"{tool}: 'old_str' matches {occurrences} times in "
                                f"'{name}' — include more surrounding context to make it unique")}
    return content.replace(old_str, new_str, 1), None


def execute_edit(slot: str, fixed: dict, params: dict,
                 output_dir: str, source_dir: str = "",
                 fallback_source_dir: str = "") -> dict:
    """Execute an edit_{slot} call: surgical str-replace on the EXISTING file.

    Baseline resolution order:
    1. ``source_dir`` (the consolidated repo — accumulated cross-task state);
    2. ``output_dir`` staging (a file written earlier this same attempt);
    3. ``fallback_source_dir`` — the step's own PROMOTED output. The caller
       only passes this for a revision loop WITHIN the current run: step dirs
       are shared across runs of one config, so an ungated fallback would let a
       fresh run silently edit a PREVIOUS run's output (e.g. chapter 2's first
       outline attempt editing chapter 1's promoted outline).

    The spliced result is always written into ``output_dir`` (staging) — from
    where normal promotion + repo_apply overwrites the repo copy.
    """
    base_name = resolve_write_target(slot, fixed, params)
    old_str = _ensure_str(params.get("old_str", ""))
    new_str = _ensure_str(params.get("new_str", ""))
    if not old_str:
        return {"error": f"edit_{slot}: 'old_str' is required and must be non-empty"}

    src_base = Path(source_dir) if source_dir else Path(output_dir)
    src = src_base / base_name
    if not src.exists():
        # fall back to staging copy (a file written earlier this same step)
        staged = Path(output_dir) / base_name
        prior = (Path(fallback_source_dir) / base_name
                 if fallback_source_dir else None)
        if staged.exists():
            src = staged
        elif prior is not None and prior.exists():
            src = prior
        else:
            return {"error": (f"edit_{slot}: cannot edit '{base_name}' — no "
                              "existing version to edit (nothing in the repo, "
                              "staging, or this run's prior output). Use "
                              f"create_{slot}/write_{slot} to author it first.")}

    content = src.read_text(encoding="utf-8")
    updated, err = _unique_replace(content, old_str, new_str,
                                   tool=f"edit_{slot}", name=base_name)
    if err:
        return err
    out = Path(output_dir) / base_name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(updated, encoding="utf-8")
    return {"edited": base_name}


def normalize_repo_path(raw: str) -> list[str]:
    """Sanitize a model-supplied write path to repo-relative components.

    Drops '.', '..', absolute leading slashes (traversal defence) AND strips a
    leading 'project/' phantom-root component (AT-9): some models prepend it
    because a dir_tree header looked like a real directory, producing
    project/pkg/x.py alongside pkg/x.py. Collapsing to a single canonical root
    keeps each logical file at one path.
    """
    parts = [p for p in Path(raw).parts
             if p not in ('.', '..') and not p.startswith('/')]
    if parts and parts[0] == "project":
        parts = parts[1:]
    return parts


def execute_generic_write(params: dict, output_dir: str) -> dict:
    """Execute a generic write(file, content) call. Sanitizes filename."""
    raw = params.get("file") or params.get("filename") or params.get("path", "")
    safe_parts = normalize_repo_path(raw)
    if not safe_parts:
        return {"error": "Invalid filename: path traversal denied"}
    path = Path(output_dir) / str(Path(*safe_parts))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ensure_str(params.get("content", "")), encoding="utf-8")
    return {"written": str(Path(*safe_parts))}


def execute_generic_create(params: dict, output_dir: str,
                           source_dir: str = "") -> dict:
    """Execute a generic create(file, content) call for new repo files.

    Writes the whole file into ``output_dir`` (staging). Refuses to create a
    file that already exists in staging or in the consolidated repo
    (``source_dir``) — existing files must be changed with ``edit``, so a
    create can never silently clobber an existing file's contents.
    """
    raw = params.get("file") or params.get("filename") or params.get("path", "")
    safe_parts = normalize_repo_path(raw)
    if not safe_parts:
        return {"error": "Invalid filename: path traversal denied"}
    rel = str(Path(*safe_parts))
    staged = Path(output_dir) / rel
    repo = (Path(source_dir) / rel) if source_dir else None
    if staged.exists() or (repo is not None and repo.exists()):
        return {"error": (f"create: '{rel}' already exists — use 'edit' to change "
                          f"an existing file (create is for new files only).")}
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(_ensure_str(params.get("content", "")), encoding="utf-8")
    return {"written": rel}


def execute_generic_edit(params: dict, output_dir: str,
                         source_dir: str = "",
                         fallback_source_dir: str = "") -> dict:
    """Execute a generic edit(file, old_str, new_str) call on an EXISTING file.

    Baseline is staging-first: read ``output_dir/file`` if a prior edit/create
    this same step already produced it (so repeated edits to one file compound),
    else the consolidated repo ``source_dir/file``, else
    ``fallback_source_dir/file`` — the step's own PROMOTED output, passed by
    the caller only for a revision loop within the current run (see
    execute_edit for the cross-run trap). Requires ``old_str`` to
    match exactly once; writes the whole spliced result into staging, from where
    promotion + repo_apply overwrites the repo copy. The repo is never edited in
    place — ``edit`` only reads it as a baseline.
    """
    raw = params.get("file") or params.get("filename") or params.get("path", "")
    safe_parts = normalize_repo_path(raw)
    if not safe_parts:
        return {"error": "Invalid filename: path traversal denied"}
    rel = str(Path(*safe_parts))
    old_str = _ensure_str(params.get("old_str", ""))
    new_str = _ensure_str(params.get("new_str", ""))
    if not old_str:
        return {"error": "edit: 'old_str' is required and must be non-empty"}

    staged = Path(output_dir) / rel
    repo = (Path(source_dir) / rel) if source_dir else None
    prior = (Path(fallback_source_dir) / rel) if fallback_source_dir else None
    if staged.exists():
        src = staged
    elif repo is not None and repo.exists():
        src = repo
    elif prior is not None and prior.exists():
        src = prior
    else:
        return {"error": (f"edit: cannot edit '{rel}' — no existing version to "
                          "edit (nothing in the repo, staging, or this run's "
                          "prior output). Use 'create' for a new file.")}

    content = src.read_text(encoding="utf-8")
    updated, err = _unique_replace(content, old_str, new_str,
                                   tool="edit", name=rel)
    if err:
        return err
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(updated, encoding="utf-8")
    return {"edited": rel}
