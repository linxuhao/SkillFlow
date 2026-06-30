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


# Default line window for generated readers when no range is given. Replaces the
# old whole-file dump so agents are never silently blind to a large file's tail;
# the ``truncated`` flag signals when there is more to page through.
_MAX_READ_LINES = 2000

# Directories never worth enumerating/searching as "source" — VCS internals and
# build/dependency caches. Mirrors dir_tree/list_tree's BLOCKED set, which the
# generated directory read tools previously lacked: a single list_repo_root on a
# real repo dumped the whole .git tree (~64% of files) into the agent's context.
_BLOCKED_DIR_PARTS = {".git", "__pycache__", ".venv", "node_modules",
                      ".mypy_cache", ".pytest_cache", ".ruff_cache"}
# Hard cap on entries returned by a directory listing so one call can't flood
# the context with thousands of paths.
_MAX_LIST_ENTRIES = 1000


def _is_blocked_path(rel) -> bool:
    """True if any path component is a blocked VCS/build directory."""
    return any(part in _BLOCKED_DIR_PARTS for part in Path(rel).parts)


def _page_lines(text: str, start_line: int = 0, end_line: int | None = None) -> dict:
    """Slice file text into a line-numbered window with paging metadata.

    ``start_line`` is 0-based (matching the ``read_file`` native tool); when
    ``end_line`` is None the window is capped at ``_MAX_READ_LINES`` lines.
    Returns content plus ``total_lines``/``returned_lines``/``truncated`` so the
    caller can page rather than be cut off without warning.
    """
    lines = text.splitlines()
    total = len(lines)
    start = start_line if isinstance(start_line, int) and start_line > 0 else 0
    start = min(start, total)
    if isinstance(end_line, int) and end_line > 0:
        end = min(end_line, total)
    else:
        end = min(start + _MAX_READ_LINES, total)
    selected = lines[start:end]
    return {
        "content": "\n".join(
            f"{start + i + 1}\t{ln}" for i, ln in enumerate(selected)
        ),
        "start_line": start,
        "returned_lines": len(selected),
        "total_lines": total,
        "truncated": end < total,
    }


# ── Path resolution ──────────────────────────────────────────────────

def _resolve_var_path(path: str, loop_context: dict | None = None) -> str:
    """Resolve ``$variable`` references in a file path against loop_context."""
    if "$" not in path or not loop_context:
        return path
    def _sub(m):
        var = m.group(1)
        return str(loop_context.get(var, loop_context.get(f"[{var}]", m.group(0))))
    return re.sub(r'\$(\w+)', _sub, path)


def resolve_context_paths(
    spec: dict,
    workspace_root: str,
    current_config: str = "",
    code_root: str = "",
    loop_context: dict | None = None,
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
                # Resolve $var references first (e.g. $current_task → backend_sessions_api)
                resolved_f = _resolve_var_path(f, loop_context)
                fp = step_dir / resolved_f
                if fp.is_file():
                    result.append(str(fp))
                elif "$" in f:
                    # Unresolved $var — try glob match (e.g. tasks/$current_task.json)
                    import glob as _glob
                    pattern = str(step_dir / re.sub(r'\$\w+', '*', f))
                    matches = sorted(_glob.glob(pattern))
                    if matches:
                        result.append(str(Path(matches[0])))
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
                    resolved_f = _resolve_var_path(f, loop_context)
                    fp = step_dir / resolved_f
                    if fp.is_file():
                        result.append(str(fp))
                    elif "$" in f:
                        import glob as _glob
                        pattern = str(step_dir / re.sub(r'\$\w+', '*', f))
                        matches = sorted(_glob.glob(pattern))
                        if matches:
                            result.append(str(Path(matches[0])))
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
                        resolved_f = _resolve_var_path(f, loop_context)
                        fp = d / resolved_f
                        if fp.is_file():
                            result.append(str(fp))
                        elif "$" in f:
                            import glob as _glob
                            pattern = str(d / re.sub(r'\$\w+', '*', f))
                            matches = sorted(_glob.glob(pattern))
                            if matches:
                                result.append(str(Path(matches[0])))
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

def _derive_label(spec: dict, loop_context: dict | None = None) -> str:
    """Derive a stable tool-name label from a context spec.

    If *loop_context* is provided, ``$variable`` references in file
    paths are resolved before deriving the label (so ``tasks/$current_task.json``
    becomes ``tasks/frontend_product.json`` instead of the literal
    ``$current_task``)."""

    source_type = spec.get("source_type", "step")

    if source_type == "step":
        step_id = spec.get("step_id", "").replace("-", "_")
        files = [_resolve_var_path(f, loop_context) for f in spec.get("files", [])]
        if len(files) == 1:
            name = Path(files[0]).stem.replace(".", "_").replace("-", "_")
            return f"step_{step_id}_{name}"
        return f"step_{step_id}"

    elif source_type == "config":
        cfg = spec.get("config_name", "").replace("-", "_")
        step_id = spec.get("step_id", "").replace("-", "_")
        files = [_resolve_var_path(f, loop_context) for f in spec.get("files", [])]
        if step_id and len(files) == 1:
            name = Path(files[0]).stem.replace(".", "_").replace("-", "_")
            return f"config_{cfg}_{step_id}_{name}"
        elif step_id:
            return f"config_{cfg}_{step_id}"
        return f"config_{cfg}"

    elif source_type == "workspace":
        path = _resolve_var_path(spec.get("path", ""), loop_context)
        name = Path(path).stem.replace(".", "_").replace("-", "_")
        return f"workspace_{name}" if name else "workspace"

    elif source_type == "repository":
        path = _resolve_var_path(spec.get("path", ""), loop_context)
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
    loop_context: dict | None = None,
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

        label = _derive_label(spec, loop_context)
        paths = resolve_context_paths(spec, workspace_root, current_config, code_root, loop_context)
        if not paths:
            continue

        source_type = spec.get("source_type", "step")
        source_desc = _source_description(spec)

        if _is_single_file(spec, paths):
            # Single file → read only
            fname = Path(paths[0]).name
            tools.append({
                "name": f"read_{label}",
                "description": f"Read {fname} from {source_desc}. Large files are "
                               f"paged: pass start_line/end_line (0-based start) "
                               f"and check the returned 'truncated'/'total_lines'.",
                "parameters": {
                    "start_line": {
                        "type": "integer",
                        "description": "0-based first line to read (optional)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Exclusive end line (optional)",
                    },
                },
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
                "description": f"Read a file from {source_desc}. Large files are "
                               f"paged: pass start_line/end_line (0-based start) "
                               f"and check the returned 'truncated'/'total_lines'.",
                "parameters": {
                    "name": {
                        "type": "string",
                        "required": True,
                        "description": f"Filename within {source_desc} (e.g. 'example.md')",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "0-based first line to read (optional)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Exclusive end line (optional)",
                    },
                },
            })
            tools.append({
                "name": f"search_{label}",
                "description": f"Search (grep) file contents in {source_desc}. "
                               f"Returns matching {{file, line, text}}; pass "
                               f"files_with_matches=true for just the file list.",
                "parameters": {
                    "pattern": {
                        "type": "string",
                        "required": True,
                        "description": "Regex (case-insensitive) or literal substring to find",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Optional filename glob filter (e.g. '*.py')",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Lines of surrounding context to include per match (default 0)",
                    },
                    "files_with_matches": {
                        "type": "boolean",
                        "description": "Return only the list of matching file paths",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max matches to return (default 50)",
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
                       loop_context: dict | None = None,
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

        label = _derive_label(spec, loop_context)
        paths = resolve_context_paths(spec, workspace_root, current_config, code_root, loop_context)
        if not paths:
            continue

        if _is_single_file(spec, paths):
            file_path = paths[0]

            def _read_single(start_line: int = 0, end_line: int | None = None,
                             _path=file_path) -> dict:
                try:
                    text = Path(_path).read_text(encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    return {"error": f"File not found: {_path}"}
                except Exception as e:
                    return {"error": str(e)}
                return _page_lines(text, start_line, end_line)

            fns[f"read_{label}"] = _read_single
        else:
            dir_paths = paths

            def _list_dir(_paths=dir_paths) -> str:
                entries = []
                truncated = False
                for dp in _paths:
                    d = Path(dp)
                    if not d.is_dir():
                        continue
                    for f in sorted(d.rglob("*")):
                        if not (f.is_file() and f.name != ".gitkeep"):
                            continue
                        rel = f.relative_to(d)
                        if _is_blocked_path(rel):
                            continue  # skip .git / build / dependency caches
                        entries.append({"name": str(rel), "size": f.stat().st_size})
                        if len(entries) >= _MAX_LIST_ENTRIES:
                            truncated = True
                            break
                    if truncated:
                        break
                return json.dumps({"files": entries, "truncated": truncated},
                                  ensure_ascii=False)

            def _read_dir_file(name: str, start_line: int = 0,
                               end_line: int | None = None, _paths=dir_paths) -> dict:
                for dp in _paths:
                    d = Path(dp)
                    if not d.is_dir():
                        continue
                    # Try exact match first, then recursive
                    candidate = d / name
                    if candidate.is_file():
                        try:
                            text = candidate.read_text(encoding="utf-8", errors="replace")
                        except Exception as e:
                            return {"error": str(e)}
                        return _page_lines(text, start_line, end_line)
                    # Recursive search
                    for f in d.rglob(name):
                        if f.is_file():
                            try:
                                text = f.read_text(encoding="utf-8", errors="replace")
                            except Exception as e:
                                return {"error": str(e)}
                            return _page_lines(text, start_line, end_line)
                return {"error": f"File not found: {name}"}

            def _search_dir(pattern: str, glob: str = None, context_lines: int = 0,
                            files_with_matches: bool = False, max_results: int = 50,
                            _paths=dir_paths) -> dict:
                try:
                    regex = re.compile(pattern, re.IGNORECASE)
                except re.error:
                    regex = None  # invalid regex → literal substring match

                cap = max_results if isinstance(max_results, int) and max_results > 0 else 50
                matches = []
                files_hit = []
                truncated = False

                for dp in _paths:
                    d = Path(dp)
                    if not d.is_dir():
                        continue
                    for f in sorted(d.rglob(glob) if glob else d.rglob("*")):
                        if not f.is_file() or f.name == ".gitkeep":
                            continue
                        if _is_blocked_path(f.relative_to(d)):
                            continue  # skip .git / build / dependency caches
                        # Skip binary-looking files
                        if f.suffix in (".pyc", ".pyo", ".so", ".o", ".bin"):
                            continue
                        try:
                            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                        except Exception:
                            continue
                        rel = str(f.relative_to(d))
                        file_matched = False
                        for li, line in enumerate(lines, 1):
                            hit = regex.search(line) if regex else (pattern.lower() in line.lower())
                            if not hit:
                                continue
                            file_matched = True
                            if files_with_matches:
                                break  # one hit is enough to list the file
                            entry = {"file": rel, "line": li, "text": line.strip()[:200]}
                            if context_lines and context_lines > 0:
                                lo = max(0, li - 1 - context_lines)
                                hi = min(len(lines), li + context_lines)
                                entry["context"] = "\n".join(
                                    f"{lo + j + 1}\t{lines[lo + j]}" for j in range(hi - lo)
                                )
                            matches.append(entry)
                            if len(matches) >= cap:
                                truncated = True
                                break
                        if file_matched:
                            files_hit.append(rel)
                            if files_with_matches and len(files_hit) >= cap:
                                truncated = True
                        if truncated:
                            break
                    if truncated:
                        break

                if files_with_matches:
                    return {"files": files_hit, "truncated": truncated}
                return {"matches": matches, "truncated": truncated}

            fns[f"list_{label}"] = _list_dir
            fns[f"read_{label}_file"] = _read_dir_file
            fns[f"search_{label}"] = _search_dir

    return fns


def get_read_tool_names(specs: list[dict], loop_context: dict | None = None) -> set[str]:
    """Return the set of tool names that would be generated from these specs.
    Doesn't resolve paths — just derives names from spec labels.
    Used for allowlist building in _execute_tool_impl.
    """
    names: set[str] = set()
    for spec in specs:
        mode = spec.get("mode", "both")
        if mode == "inline":
            continue
        label = _derive_label(spec, loop_context)
        # We don't know if it's single-file or directory without paths,
        # so add all possible names. Execution will resolve correctly.
        names.add(f"read_{label}")
        names.add(f"list_{label}")
        names.add(f"read_{label}_file")
        names.add(f"search_{label}")
    return names
