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
# build/dependency caches. Mirrors dir_tree/list_tree's BLOCKED set: without it
# a single `list`/`search` over a real repo would dump the whole .git tree
# (~64% of files) into the agent's context.
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


# ── Tool schema generation ───────────────────────────────────────────

def generate_read_tool_schemas(
    specs: list[dict],
    workspace_root: str = "",
    current_config: str = "",
    code_root: str = "",
    loop_context: dict | None = None,
    step_tmp_dir: str = "",
    step_dir: str = "",
    _smap: dict | None = None,
) -> list[dict]:
    """Generate the unified read/search/list tool schemas for a step.

    One gated trio replaces the old per-source read_{label}* tools. A step
    reads its WORKING TREE by default (own staging shadows the repo, so
    read-after-edit is consistent) and any DECLARED context source by name.
    Returns [] when the step has nothing readable. Pass ``_smap`` to reuse a
    source map already built by the caller (avoids re-resolving).
    """
    smap = _smap if _smap is not None else build_source_map(
        specs, workspace_root, current_config, code_root,
        loop_context, step_tmp_dir, step_dir)
    if not (smap["working_tree"] or smap["named"]):
        return []

    allowed = sorted(smap["allowed"])
    src_list = ", ".join(f"'{s}'" for s in allowed) or "(none)"
    src_param = {
        "type": "string",
        "description": (
            "Optional. Omit to read your WORKING TREE (your own pending "
            "create/edit output shadows the repo baseline — reads reflect what "
            f"you just wrote). Or one of: {src_list}."),
    }

    return [
        {
            "name": "read",
            "description": (
                "Read a file by repo-relative path. Omit `source` to read your "
                "working tree (your pending edits are visible). The result's "
                "`source` field names the layer that served the file. Large "
                "files page: pass start_line/end_line (0-based) and check "
                "`truncated`/`total_lines`."),
            "parameters": {
                "path": {"type": "string", "required": True,
                         "description": "Repo-relative file path (e.g. 'core/db.py')."},
                "source": src_param,
                "start_line": {"type": "integer",
                               "description": "0-based first line (optional)"},
                "end_line": {"type": "integer",
                             "description": "Exclusive end line (optional)"},
            },
        },
        {
            "name": "search",
            "description": (
                "Grep file contents. Omit `source` to search your working tree; "
                "or pass a declared source. Returns {file, line, text, source}; "
                "pass files_with_matches=true for just the file list."),
            "parameters": {
                "pattern": {"type": "string", "required": True,
                            "description": "Regex (case-insensitive) or literal substring."},
                "source": src_param,
                "glob": {"type": "string",
                         "description": "Optional filename glob filter (e.g. '*.py')"},
                "context_lines": {"type": "integer",
                                  "description": "Lines of context per match (default 0)"},
                "files_with_matches": {"type": "boolean",
                                       "description": "Return only matching file paths"},
                "max_results": {"type": "integer",
                                "description": "Max matches (default 50)"},
            },
        },
        {
            "name": "list",
            "description": (
                "List files. Omit `source` for your working tree; or pass a "
                "declared source. Returns {name, size, source}."),
            "parameters": {
                "source": src_param,
                "glob": {"type": "string",
                         "description": "Optional filename glob filter (e.g. '*.py')"},
            },
        },
    ]


# ── Unified read surface: source map + read / search / list ──────────
#
# One gated trio replaces the old per-source read_{label}* tools. A step
# reads its WORKING TREE by default — own staging (pending create/edit) →
# promoted dir → repo baseline, first-match-wins — so read-after-edit is
# consistent (no "read pristine repo → re-issue edit with a stale old_str →
# thrash" loop). It reaches any DECLARED context source by explicit `source`.
# Every result carries the `source` layer that served it.

def _source_key(spec: dict) -> str:
    """Canonical `source` argument value for a context spec."""
    st = spec.get("source_type", "step")
    if st == "repository":
        return "repo"
    if st == "step":
        return f"step:{spec.get('step_id', '?')}"
    if st == "config":
        cfg = spec.get("config_name", "?")
        step = spec.get("step_id", "")
        return f"config:{cfg}/{step}" if step else f"config:{cfg}"
    if st == "workspace":
        path = spec.get("path", "")
        return f"workspace:{path}" if path else "workspace"
    return "unknown"


def _dir_roots(paths: list[str]) -> list[str]:
    """Reduce resolved context paths to unique directory roots (a file's
    parent stands in for the file, so read/search/list see the whole
    delivered unit)."""
    roots: list[str] = []
    for p in paths:
        pp = Path(p)
        root = str(pp if pp.is_dir() else pp.parent)
        if root not in roots:
            roots.append(root)
    return roots


def _source_roots(spec: dict, workspace_root: str, current_config: str,
                  code_root: str, loop_context: dict | None) -> list[str]:
    """Directory root(s) a declared source is addressed against.

    When the source names a concrete STEP container (a ``step`` source, or a
    ``config`` source that names a step), address against that step dir: strip
    the file selectors so ``resolve_context_paths`` returns the container, and
    ``read(path, source=…)`` resolves ``path`` against the step root (natural
    paths work, same-key specs don't collapse).

    Otherwise — a stepless ``config`` output (scan-located across step dirs) or a
    ``workspace`` file — there is no single container, so a resolved file stands
    in via its parent dir (``_dir_roots``). Stripping selectors there would widen
    to the whole config/workspace tree and break same-name path addressing.
    """
    st = spec.get("source_type", "step")
    has_step_container = st == "step" or (st == "config" and spec.get("step_id"))
    if has_step_container:
        dir_spec = {k: v for k, v in spec.items()
                    if k not in ("files", "file", "output")}
        return resolve_context_paths(dir_spec, workspace_root, current_config,
                                     code_root, loop_context)
    return _dir_roots(resolve_context_paths(spec, workspace_root,
                                            current_config, code_root,
                                            loop_context))


def build_source_map(specs: list[dict], workspace_root: str,
                     current_config: str = "", code_root: str = "",
                     loop_context: dict | None = None,
                     step_tmp_dir: str = "", step_dir: str = "") -> dict:
    """Resolve a step's readable sources.

    Returns {working_tree, named, allowed}:
      working_tree — ordered [(tag, dir)] used when `source` is omitted:
                     own staging → repo. Staging-first ⇒ the agent sees its own
                     pending edits; the layers match execute_generic_edit's
                     baseline (staging→repo) so read-after-edit is consistent.
      named        — {source_key: [(tag, dir), ...]} every addressable source,
                     including 'self' (own scratch: staging→promoted) and 'repo'.
      allowed      — set of source_key strings accepted in the `source` arg.

    staging/promoted are included by PATH (not gated on is_dir): they are
    created DURING execution, and the closures' call-time is_dir/is_file guards
    handle a not-yet-created dir — so the staging-first read can't silently
    no-op before the first write.

    Read tools are offered only to a step that declares at least one non-inline
    (tool/both) context source — matching ``get_read_tool_names`` so the schema
    the agent sees and the execution allowlist agree. An all-inline step (only
    injected content, e.g. a bare dir_tree) gets no read surface.
    """
    if not any(s.get("mode", "both") != "inline" for s in (specs or [])):
        return {"working_tree": [], "named": {}, "allowed": set()}

    self_layers: list[tuple[str, str]] = [
        (tag, d) for tag, d in
        (("staging", step_tmp_dir), ("promoted", step_dir)) if d]

    working_tree: list[tuple[str, str]] = []
    if step_tmp_dir:
        working_tree.append(("staging", step_tmp_dir))
    if code_root and Path(code_root).is_dir():
        working_tree.append(("repo", code_root))

    named: dict[str, list[tuple[str, str]]] = {}
    if self_layers:
        named["self"] = self_layers
    if code_root and Path(code_root).is_dir():
        named["repo"] = [("repo", code_root)]

    for spec in specs or []:
        if spec.get("mode", "both") == "inline":
            continue
        if spec.get("source_type") == "repository":
            continue  # already mapped to 'repo'
        key = _source_key(spec)
        if key == "unknown" or key in named:
            continue
        roots = _source_roots(spec, workspace_root, current_config, code_root,
                              loop_context)
        if roots:
            named[key] = [(key, r) for r in roots]

    return {"working_tree": working_tree, "named": named,
            "allowed": set(named)}


def _layers_for(smap: dict, source):
    """Resolve the `source` arg to an ordered [(tag, dir)] list.

    Returns (layers, error_dict). error_dict is None on success; on an
    unknown source it lists the allowed sources so the agent self-corrects.
    """
    if not source:
        return smap["working_tree"], None
    if source in smap["named"]:
        return smap["named"][source], None
    return [], {"error": f"unknown source '{source}'",
                "allowed_sources": sorted(smap["allowed"]),
                "hint": "omit source to read your working tree"}


def _within(base: Path, rel: str):
    """Safe-join rel under base; None if it escapes the base dir."""
    try:
        cand = (base / rel).resolve()
    except Exception:
        return None
    b = base.resolve()
    if cand != b and b not in cand.parents:
        return None
    return cand


def _deleted_this_step(step_tmp_dir: str) -> set:
    """Repo-relative paths the agent queued for deletion this step
    (delete_file writes a bare JSON list to _deletions.json)."""
    if not step_tmp_dir:
        return set()
    f = Path(step_tmp_dir) / "_deletions.json"
    if not f.is_file():
        return set()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return set()
    items = data.get("deletions", []) if isinstance(data, dict) else data
    out = set()
    for it in items or []:
        if isinstance(it, str):
            out.add(it)
        elif isinstance(it, dict) and it.get("file"):
            out.add(it["file"])
    return out


def unified_read(smap, path, source=None, start_line=0, end_line=None,
                 deleted=None):
    layers, err = _layers_for(smap, source)
    if err:
        return err
    # Reading the working tree / own scratch reflects a deletion queued this
    # step (the repo copy still exists until repo_delete runs on deliver).
    if deleted and path in deleted and (source in (None, "", "self")):
        return {"error": f"'{path}' was deleted this step", "source": "staging"}
    for tag, root in layers:
        cand = _within(Path(root), path)
        if cand is None:
            continue
        if cand.is_file():
            try:
                text = cand.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return {"error": str(e)}
            out = _page_lines(text, start_line, end_line)
            out["source"] = tag
            out["path"] = path
            return out
    return {"error": f"File not found: {path}",
            "searched": [t for t, _ in layers]}


def unified_search(smap, pattern, source=None, glob=None, context_lines=0,
                   files_with_matches=False, max_results=50):
    layers, err = _layers_for(smap, source)
    if err:
        return err
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = None  # invalid regex → literal substring match

    cap = max_results if isinstance(max_results, int) and max_results > 0 else 50
    matches, files_hit = [], []
    seen = set()  # earlier layer wins → staging shadows repo
    truncated = False
    for tag, root in layers:
        d = Path(root)
        if not d.is_dir():
            continue
        for f in sorted(d.rglob(glob) if glob else d.rglob("*")):
            if not f.is_file() or f.name == ".gitkeep":
                continue
            rel = str(f.relative_to(d))
            if _is_blocked_path(rel):
                continue  # skip .git / build / dependency caches
            if f.suffix in (".pyc", ".pyo", ".so", ".o", ".bin"):
                continue
            if rel in seen:
                continue  # already searched the shadowing layer's copy
            seen.add(rel)
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            file_matched = False
            for li, line in enumerate(lines, 1):
                hit = regex.search(line) if regex else (pattern.lower() in line.lower())
                if not hit:
                    continue
                file_matched = True
                if files_with_matches:
                    break
                entry = {"file": rel, "line": li, "text": line.strip()[:200],
                         "source": tag}
                if context_lines and context_lines > 0:
                    lo = max(0, li - 1 - context_lines)
                    hi = min(len(lines), li + context_lines)
                    entry["context"] = "\n".join(
                        f"{lo + j + 1}\t{lines[lo + j]}" for j in range(hi - lo))
                matches.append(entry)
                if len(matches) >= cap:
                    truncated = True
                    break
            if file_matched:
                files_hit.append({"file": rel, "source": tag})
                if files_with_matches and len(files_hit) >= cap:
                    truncated = True
            if truncated:
                break
        if truncated:
            break

    if files_with_matches:
        return {"files": files_hit, "truncated": truncated}
    return {"matches": matches, "truncated": truncated}


def unified_list(smap, source=None, glob=None):
    layers, err = _layers_for(smap, source)
    if err:
        return err
    entries, seen = [], set()
    truncated = False
    for tag, root in layers:
        d = Path(root)
        if not d.is_dir():
            continue
        for f in sorted(d.rglob(glob) if glob else d.rglob("*")):
            if not (f.is_file() and f.name != ".gitkeep"):
                continue
            rel = str(f.relative_to(d))
            if _is_blocked_path(rel):
                continue
            if rel in seen:
                continue  # shadowing layer already listed it
            seen.add(rel)
            entries.append({"name": rel, "size": f.stat().st_size, "source": tag})
            if len(entries) >= _MAX_LIST_ENTRIES:
                truncated = True
                break
        if truncated:
            break
    return json.dumps({"files": entries, "truncated": truncated},
                      ensure_ascii=False)


def make_read_tool_fns(specs: list[dict], workspace_root: str = "",
                       current_config: str = "", code_root: str = "",
                       loop_context: dict | None = None,
                       step_tmp_dir: str = "", step_dir: str = "",
                       _smap: dict | None = None,
                       ) -> dict[str, callable]:
    """Build the unified read/search/list callables for a step.

    Returns {"read", "search", "list"} closures over the step's resolved
    source map (or {} when nothing is readable). Registered fresh each step,
    so the closures capture this step's staging + loop-resolved sources. Pass
    ``_smap`` to reuse a source map already built by the caller.
    """
    smap = _smap if _smap is not None else build_source_map(
        specs, workspace_root, current_config, code_root,
        loop_context, step_tmp_dir, step_dir)
    if not (smap["working_tree"] or smap["named"]):
        return {}
    deleted = _deleted_this_step(step_tmp_dir)

    def _read(path: str, source: str = None, start_line: int = 0,
              end_line: int | None = None) -> dict:
        return unified_read(smap, path, source, start_line, end_line, deleted)

    def _search(pattern: str, source: str = None, glob: str = None,
                context_lines: int = 0, files_with_matches: bool = False,
                max_results: int = 50) -> dict:
        return unified_search(smap, pattern, source, glob, context_lines,
                              files_with_matches, max_results)

    def _list(source: str = None, glob: str = None) -> str:
        return unified_list(smap, source, glob)

    return {"read": _read, "search": _search, "list": _list}


def get_read_tool_names(specs: list[dict], loop_context: dict | None = None) -> set[str]:
    """Tool names the unified read surface exposes for these specs.

    The trio (read/search/list) is granted whenever the step declares any
    non-inline context source; the step's own staging + repo baseline ride
    along as the default working tree. Used for allowlist building.
    """
    for spec in specs or []:
        if spec.get("mode", "both") != "inline":
            return {"read", "search", "list"}
    return set()
