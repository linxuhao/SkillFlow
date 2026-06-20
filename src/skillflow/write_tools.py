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
from pathlib import Path


def _ensure_str(value, default: str = "") -> str:
    """Coerce value to string for write_text. LLM native tool calling may
    pass structured objects (dict) for parameters typed as 'string'."""
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return json.dumps(value, ensure_ascii=False)


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
                                fixed: dict) -> list[dict]:
    """Generate tool schema dicts for write/create/append tools.

    Returns a list of dicts with 'name', 'description', 'parameters'.
    """
    if output_mode == "write" and not fixed:
        return [{
            "name": "write",
            "description": "Write a file. The 'file' param must be a bare filename.",
            "parameters": {
                "file": {"type": "string", "required": True},
                "content": {"type": "string", "required": True},
            },
        }, {
            "name": "finish_step",
            "description": (
                "Signal that all required output files have been written and "
                "the step is complete. Call this ONLY after all write tool calls "
                "in the current turn have been made — it must be the last call."
            ),
            "parameters": {
                "summary": {"type": "string", "required": False,
                           "description": "Brief summary of what was created or completed"},
            },
        }]

    if output_mode == "content":
        tools = []
        for slot, entry in fixed.items():
            normalized = _normalize_fixed_entry(entry)
            pattern = normalized["file"]
            format_spec = normalized.get("format")
            fmt_hint = f"\nExpected format: {format_spec.strip()}" if format_spec else ""
            is_glob = "*" in pattern

            # Shared params
            if is_glob:
                id_desc = f"Replaces * in {pattern}"
                # When the format declares an "id" field, the parameter value
                # should match it — otherwise the LLM fills in a placeholder
                # like "unknown" and all task cards land in the same file.
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
            if "content" in create_params:
                create_params["initialContent"] = create_params.pop("content")

            # write_{slot} — full replace
            tools.append({
                "name": f"write_{slot}",
                "description": f"Replace {pattern} with new content.{fmt_hint}",
                "parameters": params,
            })

            # create_{slot} — create or archive-and-create (flipped rename)
            tools.append({
                "name": f"create_{slot}",
                "description": (
                    f"Create {pattern} with initial content. "
                    f"If file already exists, it is archived with a numeric suffix, "
                    f"so {pattern} always holds the latest version.{fmt_hint}"
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

    if mode == "new":
        # Flipped rename: archive old file, write new content to canonical name
        archived = _archive_old_file(directory, base_name)
        filename = base_name
    elif mode == "append":
        filename = base_name
        path = directory / filename
        content = _ensure_str(params.get("content", ""))
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(existing + content, encoding="utf-8")
        return {"written": filename}
    else:  # replace
        filename = base_name

    path = directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _ensure_str(params.get("content") or params.get("initialContent", ""))
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
    archived = _archive_old_file(directory, base_name)
    path = directory / base_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ensure_str(params.get("content") or params.get("initialContent", "")), encoding="utf-8")
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


def execute_edit(slot: str, fixed: dict, params: dict,
                 output_dir: str, source_dir: str = "") -> dict:
    """Execute an edit_{slot} call: surgical str-replace on the EXISTING file.

    Reads the current file from ``source_dir`` (the consolidated repo, so the
    edit applies to accumulated cross-task state), applies a single exact
    replacement, and writes the result into ``output_dir`` (the step's staging
    dir) — from where normal promotion + repo_apply overwrites the repo copy.
    Falls back to ``output_dir`` as the source when no repo dir is given.
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
        if staged.exists():
            src = staged
        else:
            return {"error": f"edit_{slot}: cannot edit '{base_name}' — file does not exist"}

    content = src.read_text(encoding="utf-8")
    occurrences = content.count(old_str)
    if occurrences == 0:
        return {"error": f"edit_{slot}: 'old_str' not found in '{base_name}'"}
    if occurrences > 1:
        return {"error": (f"edit_{slot}: 'old_str' matches {occurrences} times in "
                          f"'{base_name}' — include more surrounding context to make it unique")}

    updated = content.replace(old_str, new_str, 1)
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
