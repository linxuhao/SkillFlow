"""Dynamic read tool generation from context specs.

Mirrors write_tools.py: each context entry with mode ∈ {tool, both}
generates read/list/search tool schemas.  Tool functions are closures
over pre-resolved absolute paths — the agent never sees or guesses paths.

Generated tools by granularity:

Single file (files: [name]) → ``read_{label}() → str``
Directory (no files filter) → ``list_{label}() → [FileEntry]``
                             ``read_{label}_file(name: str) → str``
                             ``search_{label}(pattern: str) → [Match]``
Repository (from: repository) → same as directory, subject to code root.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path


# ── Path resolution ──────────────────────────────────────────────────

def resolve_context_paths(
    spec: dict,
    workspace_root: str,
    current_config: str = "",
    code_root: str = "",
) -> list[str]:
    """Resolve a normalized context spec to a list of absolute paths.

    Returns a list where:
    - Single file: one element (the file path)
    - Directory: one element (the directory path)
    - Empty list: source not found
    """
    source_type = spec.get("source_type", "step")
    ws = Path(workspace_root)

    if source_type == "step":
        step_id = spec.get("step_id", "")
        cfg = current_config or "dpe_default"
        step_dir = ws / cfg / step_id
        if not step_dir.is_dir():
            return []
        files = spec.get("files", [])
        if files:
            # Single file: return the specific file paths
            result = []
            for f in files:
                fp = step_dir / f
                if fp.is_file():
                    result.append(str(fp))
            return result
        else:
            return [str(step_dir)]

    elif source_type == "config":
        config_name = spec.get("config_name", "")
        step_id = spec.get("step_id", "")
        cfg_dir = ws / config_name
        if not cfg_dir.is_dir():
            return []
        files = spec.get("files", [])
        if step_id:
            # Specific step within the config
            step_dir = cfg_dir / step_id
            if not step_dir.is_dir():
                return []
            if files:
                result = []
                for f in files:
                    fp = step_dir / f
                    if fp.is_file():
                        result.append(str(fp))
                return result
            return [str(step_dir)]
        else:
            # Scan all step dirs in the config for matching files
            if files:
                result = []
                for d in sorted(cfg_dir.iterdir()):
                    if not d.is_dir() or d.name.endswith(".tmp"):
                        continue
                    for f in files:
                        fp = d / f
                        if fp.is_file():
                            result.append(str(fp))
                return result
            return [str(cfg_dir)]

    elif source_type == "workspace":
        rel_path = spec.get("path", "")
        abs_path = ws / rel_path
        if abs_path.is_file():
            return [str(abs_path)]
        elif abs_path.is_dir():
            return [str(abs_path)]
        return []

    elif source_type == "repository":
        if not code_root:
            return []
        rel_path = spec.get("path", "")
        abs_path = Path(code_root) / rel_path if rel_path else Path(code_root)
        if abs_path.is_dir():
            return [str(abs_path)]
        elif abs_path.is_file():
            return [str(abs_path)]
        return []

    return []


# ── Label derivation ─────────────────────────────────────────────────

def _derive_label(spec: dict) -> str:
    """Derive a stable tool-name label from a context spec."""
    source_type = spec.get("source_type", "step")

    if source_type == "step":
        step_id = spec.get("step_id", "").replace("-", "_")
        files = spec.get("files", [])
        if len(files) == 1:
            name = Path(files[0]).stem.replace(".", "_").replace("-", "_")
            return f"step_{step_id}_{name}"
        return f"step_{step_id}"

    elif source_type == "config":
        cfg = spec.get("config_name", "").replace("-", "_")
        step_id = spec.get("step_id", "").replace("-", "_")
        files = spec.get("files", [])
        if step_id and len(files) == 1:
            name = Path(files[0]).stem.replace(".", "_").replace("-", "_")
            return f"config_{cfg}_{step_id}_{name}"
        elif step_id:
            return f"config_{cfg}_{step_id}"
        return f"config_{cfg}"

    elif source_type == "workspace":
        path = spec.get("path", "")
        name = Path(path).stem.replace(".", "_").replace("-", "_")
        return f"workspace_{name}" if name else "workspace"

    elif source_type == "repository":
        path = spec.get("path", "")
        name = Path(path).stem.replace(".", "_").replace("-", "_") if path else "root"
        return f"repo_{name}"

    return "unknown"


# ── Tool schema generation ───────────────────────────────────────────

def _is_single_file(spec: dict, paths: list[str]) -> bool:
    """True if paths point to individual files (not directories)."""
    if not paths:
        return False
    return all(Path(p).is_file() for p in paths)


def generate_read_tool_schemas(
    specs: list[dict],
    workspace_root: str,
    current_config: str = "",
    code_root: str = "",
) -> list[dict]:
    """Generate read tool schema dicts from normalized context specs.

    Only generates tools for specs where mode ∈ {tool, both}.

    Returns list of {name, description, parameters} dicts suitable for
    merging into _tool_schemas.
    """
    tools: list[dict] = []

    for spec in specs:
        mode = spec.get("mode", "both")
        if mode == "inline":
            continue

        label = _derive_label(spec)
        paths = resolve_context_paths(spec, workspace_root, current_config, code_root)
        if not paths:
            continue

        source_type = spec.get("source_type", "step")
        source_desc = _source_description(spec)

        if _is_single_file(spec, paths):
            # Single file → read only
            fname = Path(paths[0]).name
            tools.append({
                "name": f"read_{label}",
                "description": f"Read {fname} from {source_desc}.",
                "parameters": {},
            })
        else:
            # Directory → list + read + search
            dir_path = paths[0]
            tools.append({
                "name": f"list_{label}",
                "description": f"List all files in {source_desc}.",
                "parameters": {},
            })
            tools.append({
                "name": f"read_{label}_file",
                "description": f"Read a file from {source_desc}.",
                "parameters": {
                    "name": {
                        "type": "string",
                        "required": True,
                        "description": f"Filename within {source_desc} (e.g. 'example.md')",
                    },
                },
            })
            tools.append({
                "name": f"search_{label}",
                "description": f"Search within all files in {source_desc}.",
                "parameters": {
                    "pattern": {
                        "type": "string",
                        "required": True,
                        "description": "Search term or regex pattern to find",
                    },
                },
            })

    return tools


def _source_description(spec: dict) -> str:
    """Human-readable description of the source for tool descriptions."""
    source_type = spec.get("source_type", "step")
    if source_type == "step":
        return f"Step {spec.get('step_id', '?')}'s output"
    elif source_type == "config":
        cfg = spec.get("config_name", "?")
        step = spec.get("step_id", "")
        return f"Config '{cfg}'" + (f" Step '{step}'" if step else "")
    elif source_type == "workspace":
        return f"project workspace ({spec.get('path', 'root')})"
    elif source_type == "repository":
        return f"code repository ({spec.get('path', 'root')})"
    return "unknown source"


# ── Tool execution functions (closures over resolved paths) ──────────

def make_read_tool_fns(specs: list[dict], workspace_root: str,
                       current_config: str = "", code_root: str = "",
                       ) -> dict[str, callable]:
    """Create execution functions for all read tools from context specs.

    Returns a dict of tool_name → callable.  Each callable accepts kwargs
    matching the corresponding tool schema parameters and returns a result dict
    or string.

    Only generates functions for specs where mode ∈ {tool, both}.
    """
    fns: dict[str, callable] = {}

    for spec in specs:
        mode = spec.get("mode", "both")
        if mode == "inline":
            continue

        label = _derive_label(spec)
        paths = resolve_context_paths(spec, workspace_root, current_config, code_root)
        if not paths:
            continue

        if _is_single_file(spec, paths):
            file_path = paths[0]

            def _read_single(_path=file_path) -> str:
                try:
                    return Path(_path).read_text(encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    return json.dumps({"error": f"File not found: {_path}"})
                except Exception as e:
                    return json.dumps({"error": str(e)})

            fns[f"read_{label}"] = _read_single
        else:
            dir_paths = paths

            def _list_dir(_paths=dir_paths) -> str:
                entries = []
                for dp in _paths:
                    d = Path(dp)
                    if not d.is_dir():
                        continue
                    for f in sorted(d.rglob("*")):
                        if f.is_file() and f.name != ".gitkeep":
                            rel = f.relative_to(d)
                            st = f.stat()
                            entries.append({
                                "name": str(rel),
                                "size": st.st_size,
                            })
                return json.dumps(entries, ensure_ascii=False)

            def _read_dir_file(name: str, _paths=dir_paths) -> str:
                for dp in _paths:
                    d = Path(dp)
                    if not d.is_dir():
                        continue
                    # Try exact match first, then recursive
                    candidate = d / name
                    if candidate.is_file():
                        try:
                            return candidate.read_text(encoding="utf-8", errors="replace")
                        except Exception as e:
                            return json.dumps({"error": str(e)})
                    # Recursive search
                    for f in d.rglob(name):
                        if f.is_file():
                            try:
                                return f.read_text(encoding="utf-8", errors="replace")
                            except Exception as e:
                                return json.dumps({"error": str(e)})
                return json.dumps({"error": f"File not found: {name}"})

            def _search_dir(pattern: str, _paths=dir_paths) -> str:
                matches = []
                try:
                    regex = re.compile(pattern, re.IGNORECASE)
                except re.error:
                    # Treat as literal substring
                    regex = None

                for dp in _paths:
                    d = Path(dp)
                    if not d.is_dir():
                        continue
                    for f in sorted(d.rglob("*")):
                        if not f.is_file() or f.name == ".gitkeep":
                            continue
                        # Skip binary-looking files
                        if f.suffix in (".pyc", ".pyo", ".so", ".o", ".bin"):
                            continue
                        try:
                            content = f.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            continue
                        for li, line in enumerate(content.splitlines(), 1):
                            hit = False
                            if regex:
                                if regex.search(line):
                                    hit = True
                            elif pattern.lower() in line.lower():
                                hit = True
                            if hit:
                                matches.append({
                                    "file": str(f.relative_to(d)),
                                    "line": li,
                                    "text": line.strip()[:200],
                                })
                # Limit to avoid huge responses
                if len(matches) > 50:
                    matches = matches[:50]
                    matches.append({"truncated": True, "note": "Results capped at 50 matches"})
                return json.dumps(matches, ensure_ascii=False)

            fns[f"list_{label}"] = _list_dir
            fns[f"read_{label}_file"] = _read_dir_file
            fns[f"search_{label}"] = _search_dir

    return fns


def get_read_tool_names(specs: list[dict]) -> set[str]:
    """Return the set of tool names that would be generated from these specs.
    Doesn't resolve paths — just derives names from spec labels.
    Used for allowlist building in _execute_tool_impl.
    """
    names: set[str] = set()
    for spec in specs:
        mode = spec.get("mode", "both")
        if mode == "inline":
            continue
        label = _derive_label(spec)
        # We don't know if it's single-file or directory without paths,
        # so add all possible names. Execution will resolve correctly.
        names.add(f"read_{label}")
        names.add(f"list_{label}")
        names.add(f"read_{label}_file")
        names.add(f"search_{label}")
    return names
