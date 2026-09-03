"""Context resolution from step config context specs.

Resolves ``context`` entries from a step node's config into assembled
content for prompt injection. Supports six source types:

- ``{config: "name", output: "file"}`` — cross-config read
- ``{config: "name", step: "id", output: "file"}`` — cross-config from specific step
- ``{step: "id", file: "name", mode: "full"|"summary"|"interfaces"}`` — same-config read
- ``{from: "repository", path: "dir/", mode: "index"}`` — directory INDEX only:
  one line per file (name, bytes, first headings), no content; the agent reads
  what it needs with its read tool. For big append-only ledgers that a step
  must know EXIST but rarely needs whole.
- ``{step: "id"}`` — all files from that step's directory
- ``{feedback_of: "id"}`` — accumulated checkpoint-feedback log of another step
- ``{tool: "name"}`` — dynamic tool call (e.g. dir_tree)
"""

from __future__ import annotations

import logging
from pathlib import Path

# ── Checkpoint-feedback log (written by core.reject_checkpoint) ─────────────
# One file per step at {config}/_feedback/{step}.md, appended round by round.
# The read contract below exists because of two observed failure modes, both
# the same root error — reading META text (talk ABOUT the artifact) as OBJECT
# text (the artifact itself):
#   1. feedback QUOTED the offending passage; the next revision pasted the
#      quote back verbatim, precisely reverting an earlier round's fix;
#   2. feedback stated a CONSTRAINT ("deaths cost no stat points"); the next
#      revision transcribed it into the artifact ("...no stat points are
#      deducted") — announcing compliance instead of complying. The code
#      equivalent is a "// no globals used here" comment answering "don't use
#      globals".
# The preamble is prepended at READ time only; the file on disk stays clean
# (append logic counts rounds from the raw file).
FEEDBACK_LOG_PREAMBLE = (
    "[How to read this feedback log]\n"
    "- Rounds are CUMULATIVE. Every round below is still binding: fixing the "
    "latest round must NOT undo what an earlier round demanded. Before "
    "submitting, re-check your output against each round in turn.\n"
    "- Quoted passages inside a round are the OLD text being complained "
    "about, captured when that feedback was given. They locate the problem — "
    "they are NOT text to reproduce. A later revision may already have fixed "
    "them; check the current version first, and never copy a quoted passage "
    "back into your output.\n"
    "- Feedback is a CONSTRAINT on the artifact, not text to put IN it. You "
    "satisfy it by what the artifact IS, never by restating it. In "
    "particular, do not assert the absence of something to prove compliance "
    "with \"don't do X\" — a reader who never knew X was possible only learns "
    "it exists from your denial. Just leave X out.\n"
)

# Byte cap on an `interfaces` extract. Exceeding it is ANNOUNCED, never
# silent — see _extract_interfaces for why an unmarked cut is worse than
# a visible one.
_INTERFACES_MAX_CHARS = 8000

# The file-boundary marker inside a multi-file context entry.
#
# `### FILE: <relpath>` and not a bare `### <relpath>`: the bare form is valid
# markdown, so it cannot be told apart from a section heading in the CONTENT of
# the files being concatenated, and every rule for guessing trades one failure
# mode for the other. Both were live in AItelier's context clipper — requiring
# an extension made "### Dockerfile" invisible (its content folded into the
# previous file, which was then reported at the wrong length), while accepting
# any bare word made "### Notes" invent a file that does not exist and split a
# real one. An explicit token has neither, by construction.
_FILE_MARKER = "### FILE: "


def feedback_log_path(config_dir: Path, step_id: str) -> Path:
    """Path of a step's accumulated checkpoint-feedback log."""
    return config_dir / "_feedback" / f"{step_id}.md"


def read_feedback_log(config_dir: Path, step_id: str) -> str | None:
    """Read a step's feedback log, prefixed with the read-contract preamble.

    Returns None when the log is absent, unreadable, or blank — callers treat
    that as "no feedback to inject".
    """
    p = feedback_log_path(config_dir, step_id)
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.strip():
        return None
    return FEEDBACK_LOG_PREAMBLE + "\n" + text


# Ceiling on ONE directory source's concatenated text, in BYTES. Beyond it the source is
# cut with a marker naming what was dropped: the step's persisted inputs are
# where this lands, and an unbounded source has already produced single rows of
# 89 MB. Generous on purpose — this is a runaway guard, not a context budget.
_DIR_SOURCE_MAX_BYTES = 2_000_000

_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff",
    ".mp3", ".wav", ".ogg", ".flac", ".mp4", ".mov", ".webm", ".avi",
    ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".ttf", ".otf", ".woff", ".woff2", ".so", ".dll", ".dylib",
    ".pyc", ".pyo", ".class", ".jar", ".wasm", ".db", ".sqlite", ".bin",
    ".pck", ".import", ".res", ".exr", ".psd", ".ppm", ".pgm", ".pbm",
}


def _is_binary(path) -> bool:
    """Whether a file should be excluded from a text context source.

    Extension first (cheap and covers the common case), then a NUL-byte sniff of
    the first 8 KB for anything unknown — a decoded binary is never useful to a
    reader and, unlike a long document, it cannot even be truncated into
    something meaningful.
    """
    if path.suffix.lower() in _BINARY_SUFFIXES:
        return True
    try:
        with open(path, "rb") as fh:
            sample = fh.read(8192)
    except Exception:
        return True
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    # Header-then-raw formats (.ppm, .bmp, some dumps) start with ASCII and may
    # carry no NUL at all in the first 8 KB. Judge by how much of the sample is
    # not plausible text: a 30% floor passes UTF-8 prose in any language while
    # rejecting pixel data.
    text_bytes = bytes(range(0x20, 0x7F)) + b"\t\r\n\f\b"
    nontext = sum(1 for b in sample if b not in text_bytes and b < 0x80)
    return nontext / len(sample) > 0.30



_SUMMARY_MAX_CHARS = 6_000
_INDEX_MAX_HEADINGS = 3


def _dir_index(files: list, root) -> str:
    """One line per file — relative name, size in bytes, first Markdown headings.

    Order is the caller's (``_apply_order`` already ran), so the index reads in
    priority order like the inline bundle would. Headings are the cheapest
    honest summary of a Markdown file: they tell the reader what it would find
    without the host paying for the body. Non-Markdown files get name + size.
    """
    lines = []
    total = 0
    for f in files:
        try:
            nbytes = f.stat().st_size
        except OSError:
            continue
        total += nbytes
        rel = f.relative_to(root).as_posix()
        heads: list[str] = []
        if f.suffix.lower() in (".md", ".markdown"):
            try:
                with f.open(encoding="utf-8", errors="replace") as fh:
                    for ln in fh:
                        if ln.startswith("#"):
                            heads.append(ln.strip()[:80])
                            if len(heads) >= _INDEX_MAX_HEADINGS:
                                break
            except OSError:
                pass
        line = f"- {rel}  ({nbytes} bytes)"
        if heads:
            line += "  —  " + " | ".join(heads)
        lines.append(line)
    if not lines:
        return ""
    head = (f"[index: {len(lines)} file(s), {total} bytes total — contents NOT "
            f"inlined; read a file with your read tool when you need it]")
    return head + "\n" + "\n".join(lines)


def _apply_order(files: list, root, order: list) -> list:
    """Put the names in ``order`` first, in that order; the rest keep sorted order.

    An inline directory bundle is cut from the END by the host's prompt budget,
    so the file order IS the priority order. Without this the priority is the
    alphabet, which encodes nothing: AItelier's design/ handed its planners the
    907-line content catalogue whole (sorts early) and dropped the 919-line
    decisions record entirely (sorts late), measured 2026-09-01.

    Names are matched against each file's path relative to ``root``, in posix
    form, so both "90_decisions.md" and "sub/dir/notes.md" work.

    A listed name that matches nothing is skipped, never raised: the list lives
    in a config while the directory keeps changing, and a renamed doc must not
    fail the run. It IS logged — an ordering feature that quietly does nothing
    is the same shape as the truncation it exists to steer.
    """
    if not order:
        return files
    by_rel = {f.relative_to(root).as_posix(): f for f in files}
    first, seen, missing = [], set(), []
    for name in order:
        key = str(name).strip().replace("\\", "/")
        # NOT lstrip("./") — lstrip takes a CHARACTER SET, so it ate the
        # leading dot of any dotfile: ".env.md" became "env.md", matched
        # nothing, and warned as missing. A silent no-op inside the one
        # feature whose whole point is never to silently do nothing.
        while key.startswith("./"):
            key = key[2:]
        f = by_rel.get(key)
        if f is None:
            missing.append(name)
            continue
        if key not in seen:
            seen.add(key)
            first.append(f)
    if missing:
        logging.getLogger("skillflow.context").warning(
            "context source order lists %d name(s) not present under %s: %s "
            "— ignored, the remaining order still applies",
            len(missing), root, ", ".join(str(m) for m in missing))
    return first + [f for f in files
                    if f.relative_to(root).as_posix() not in seen]

class ContextResolver:
    """Resolves context sources into assembled content."""

    def __init__(self, workspace_root: Path, tool_loader=None,
                 code_root: Path | str | bool | None = None,
                 extra_tool_kwargs: dict | None = None):
        self._workspace_root = Path(workspace_root)
        self._tool_loader = tool_loader
        # Capability context of the READING step: a context-source tool
        # (`{source: {tool: X}}`) is invoked on behalf of the step whose context
        # this is, so it must receive that step's `capability` kwargs (e.g. a
        # durable state_dir) like every other tool path. Signature-filtered at
        # the call so a tool that doesn't declare them is unaffected.
        self._extra_tool_kwargs = dict(extra_tool_kwargs or {})
        # The CODE repository root (workspace.get_project_code_path). Before
        # this existed, the inline branch of `from: repository` (and context-
        # source tools' project_root) silently used workspace_root/"project" —
        # a near-empty brief dir — while the read-tool branch of the SAME spec
        # used the real code repo. Falls back to the old path so a resolver
        # constructed without it keeps its (degenerate) behavior.
        #
        # ``False`` is the third answer, matching WorkspaceManager's
        # ``code_path_resolver`` convention: "this run HAS no code repository".
        # It must not fall back, because the fallback path
        # (``workspace_root/"project"``) exists and is populated — it is the
        # project BRIEF directory — so a repo-less run would be shown the brief
        # dir labelled "Repository". None keeps meaning "no opinion".
        if code_root is False:
            self._code_root = None
        else:
            self._code_root = Path(code_root) if code_root \
                else self._workspace_root / "project"

    def resolve(self, specs: list[dict],
                current_config: str = "",
                loop_context: dict[str, str] | None = None) -> dict[str, str]:
        """Resolve a list of context specs into a dict of label→content.

        Returns a dict keyed by human-readable labels (e.g. "Project Brief",
        "Architecture Design") suitable for prompt assembly.

        If *loop_context* is provided, ``$variable`` references in ``file:``
        fields are substituted with the corresponding loop variable values.

        Ordering: entries are emitted by **volatility tier** — static reads
        (config/repository) first, then step outputs, then volatile sources
        (workspace/tool, e.g. dir_tree) last — preserving declaration order
        within each tier. This keeps the slow-changing context at the front so
        a host can place it in the prompt's cacheable prefix; volatile tool
        outputs that change every run never poison that prefix. (Reviewer /
        user feedback and validation errors are appended by the framework
        *after* this dict, so they remain strictly last.)
        """
        # Stable tier-sort: (tier, original_index) keeps within-tier order.
        ordered = sorted(
            enumerate(specs),
            key=lambda iv: (self._volatility_tier(iv[1].get("source", iv[1])), iv[0]),
        )
        result: dict[str, str] = {}
        for _, spec in ordered:
            source = spec.get("source", spec)
            label, content = self._resolve_one(source, current_config, loop_context)
            if content:
                result[label] = content
            elif spec.get("required") or source.get("required"):
                # A required input resolved to nothing → fail the step loudly
                # instead of running on absent context (which invites hallucinated
                # output). See exceptions.RequiredContextMissing.
                from skillflow.exceptions import RequiredContextMissing
                desc = label or source.get("step") or source.get("config") \
                    or source.get("path") or source.get("source_type") or str(source)
                raise RequiredContextMissing(
                    f"Required context source resolved to no content: {desc}. "
                    "The step cannot run without it.")
        return result

    @staticmethod
    def _volatility_tier(source: dict) -> int:
        """Cache-stability tier of a context source (lower = more stable).

        0 = static reads (config/repository), 1 = step outputs,
        2 = volatile (workspace/tool, e.g. dir_tree).
        """
        source_type = source.get("source_type", "")
        if "feedback_of" in source:
            return 2  # changes on every reject round — keep out of the cache prefix
        if source_type in ("config", "repository") or "config" in source:
            return 0
        if source_type == "step" or "step" in source:
            return 1
        return 2  # workspace, tool, or unknown — treat as volatile

    def _resolve_one(self, source: dict, current_config: str,
                     loop_context: dict[str, str] | None = None) -> tuple[str, str]:
        source_type = source.get("source_type", "")
        if "feedback_of" in source:
            return self._resolve_feedback(source, current_config)
        if source_type == "config" or "config" in source:
            return self._resolve_cross_config(source, current_config)
        if source_type == "step" or "step" in source:
            return self._resolve_step_output(source, current_config, loop_context)
        if source_type == "workspace":
            return self._resolve_workspace(source)
        if source_type == "repository":
            return self._resolve_repository(source)
        if "tool" in source:
            return self._resolve_tool(source, current_config)
        return "", ""

    def _resolve_cross_config(self, source: dict, current_config: str) -> tuple[str, str]:
        config_name = source["config"]
        output_file = source["output"]
        cfg_dir = self._workspace_root / config_name
        if not cfg_dir.exists():
            return "", ""

        # If step is specified, read from that step's directory
        if "step" in source:
            step_dir = cfg_dir / source["step"]
            if step_dir.exists() and step_dir.is_dir():
                file_path = step_dir / output_file
                if file_path.exists():
                    try:
                        content = file_path.read_text(encoding="utf-8", errors="replace")
                        label = f"{config_name}/{source['step']}/{output_file}"
                        return label, content
                    except Exception:
                        logger = logging.getLogger("skillflow.context")
                        logger.debug("Failed to read %s", file_path, exc_info=True)
                        return "", ""

        # Otherwise scan all step dirs (new-style) and legacy Outbox_Final_* dirs
        for d in sorted(cfg_dir.glob("*")):
            if d.name.endswith(".tmp") or d.name.startswith("Outbox_Draft"):
                continue
            if not d.is_dir():
                continue
            file_path = d / output_file
            if file_path.exists():
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                    label = f"{config_name}/{output_file}"
                    return label, content
                except Exception:
                    continue

        return "", ""

    def _resolve_step_output(self, source: dict, current_config: str,
                              loop_context: dict[str, str] | None = None) -> tuple[str, str]:
        import re

        step_id = source["step"]
        output_file = source.get("output") or source.get("file")
        mode = source.get("mode", "full")
        cfg = current_config or "dpe_default"

        # Substitute $variable references from loop context
        if output_file and "$" in output_file and loop_context:
            def _sub(m):
                var_name = m.group(1)
                # Look up both [var_name] and plain var_name keys
                return loop_context.get(f"[{var_name}]",
                       loop_context.get(var_name, m.group(0)))
            output_file = re.sub(r'\$(\w+)', _sub, output_file)

        # New path: workspace/{project}/{config}/{step_id}/
        base_dir = self._workspace_root / cfg / step_id

        # Per-item routing lives in ONE place (workspace.route_step_read_dir):
        # a same-loop reader gets this item's {step}/{item}/; every other reader
        # (aggregators, other loops) gets the {step}/ parent — all items.
        from skillflow.workspace import route_step_read_dir
        step_dir = route_step_read_dir(base_dir, step_id,
                                       source.get("scope", "task"), loop_context)
        # Routed to the parent of a per-item producer → file selectors must also
        # search each {item}/ subfolder (flat lookups still honored for
        # pre-per-item layouts).
        all_items_mode = (step_dir == base_dir and loop_context
                          and step_id in (loop_context.get("_loop_of") or {}))

        if not step_dir.exists() or not step_dir.is_dir():
            return "", ""

        # No specific file requested — return all files concatenated
        if not output_file:
            parts: list[str] = []
            entries: list[tuple[str, int, int]] = []
            skipped: list[str] = []
            for f in sorted(step_dir.rglob("*")):
                if f.is_file() and f.name != ".gitkeep":
                    if _is_binary(f):
                        # A step's output dir is not all prose. One Godot
                        # play-test step writes 184 PNG frames (101 MB); decoded
                        # with errors="replace" they became 91 MB of replacement
                        # characters inside ONE step's persisted inputs — a
                        # single row larger than most databases, that no reader
                        # could use and no prompt could hold. Named, not hidden:
                        # the reader has to know the file is there.
                        skipped.append(str(f.relative_to(step_dir)))
                        continue
                    try:
                        content = f.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    rel = f.relative_to(step_dir)
                    projected = self._project(content, mode, str(rel))
                    entries.append((str(rel), len(content), len(projected)))
                    parts.append(f"{_FILE_MARKER}{rel}\n{projected}")
            if not parts and not skipped:
                return "", ""
            label = f"Step {step_id}"
            body = "\n\n".join(parts)
            if skipped:
                shown = ", ".join(skipped[:10])
                more = f" (+{len(skipped) - 10} more)" if len(skipped) > 10 else ""
                body = (f"[binary files in this step's output, not shown: "
                        f"{shown}{more}]\n\n" + body)
            # A directory that projected nothing is byte-identical to before —
            # the header appears only where something was actually cut, so a
            # config that asks for no projection sees no change at all (which
            # matters: this block feeds provider prefix caches).
            if any(raw != proj for _, raw, proj in entries):
                body = self._dir_listing(entries) + "\n" + body
            # BYTES, not characters. A character cap is ~3x looser than it
            # reads for CJK, and this exists to bound what lands in a step's
            # persisted inputs — which is measured in bytes.
            nbytes = len(body.encode("utf-8", errors="ignore"))
            if nbytes > _DIR_SOURCE_MAX_BYTES:
                keep = body.encode("utf-8", errors="ignore")[:_DIR_SOURCE_MAX_BYTES]
                body = (keep.decode("utf-8", errors="ignore")
                        + f"\n\n... ⚠️ TRUNCATED — this step's output directory "
                          f"is {nbytes} bytes, over the {_DIR_SOURCE_MAX_BYTES} "
                          f"limit for one context source. Files after this point "
                          f"are NOT shown; read them directly, or name the file "
                          f"you need with `output:`.")
            return label, body

        # Specific file: glob for patterns like "tasks/*.json"
        if "*" in output_file:
            parts = []
            matches = sorted(step_dir.glob(output_file))
            if all_items_mode:
                matches = sorted(set(matches) | set(step_dir.glob(f"*/{output_file}")))
            for f in matches:
                if f.is_file():
                    if _is_binary(f):
                        # `tasks/*` can match a PNG as easily as a directory can.
                        continue
                    try:
                        content = f.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    rel = f.relative_to(step_dir)
                    parts.append(f"{_FILE_MARKER}{rel}\n"
                                 f"{self._project(content, mode, str(rel))}")
            if not parts:
                return "", ""
            label = f"Step {step_id} — {output_file}"
            return label, "\n\n".join(parts)

        file_path = step_dir / output_file
        if not file_path.exists() and all_items_mode:
            # Aggregate the named file across every item folder.
            per_item = sorted(step_dir.glob(f"*/{output_file}"))
            parts = []
            for f in per_item:
                if f.is_file():
                    try:
                        c = f.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    parts.append(f"{_FILE_MARKER}{f.relative_to(step_dir)}\n{c}")
            if parts:
                return f"Step {step_id} — {output_file} (all items)", "\n\n".join(parts)
            return "", ""
        if not file_path.exists():
            return "", ""

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return "", ""

        content = self._project(content, mode, output_file)

        label = f"Step {step_id} — {output_file}"
        return label, content

    def _resolve_feedback(self, source: dict,
                          current_config: str) -> tuple[str, str]:
        """``{feedback_of: "step"}`` — another step's accumulated
        checkpoint-feedback log (same config only).

        Reject feedback is otherwise injected ONLY into the rejected step's own
        re-run, so a reviewer stays blind to what the user demanded — a
        revision that silently reverts an earlier round's fix passes review
        unchallenged. Wiring this source onto the reviewer closes that hole.
        """
        step_id = source.get("feedback_of", "")
        cfg = current_config or "dpe_default"
        if not step_id:
            return "", ""
        content = read_feedback_log(self._workspace_root / cfg, step_id)
        if not content:
            return "", ""
        label = (f"⚠️ User feedback on step '{step_id}' "
                 "(all rounds — MUST still be satisfied)")
        return label, content

    def _resolve_workspace(self, source: dict) -> tuple[str, str]:
        """Resolve from: workspace source — read from project workspace root."""
        rel_path = source.get("path", "")
        abs_path = self._workspace_root / rel_path
        if abs_path.is_file():
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                label = f"Workspace — {rel_path}"
                return label, content
            except Exception:
                return "", ""
        elif abs_path.is_dir():
            # Directory: concatenate all files (like step dir)
            files = [f for f in sorted(abs_path.rglob("*"))
                     if f.is_file() and f.name != ".gitkeep"]
            files = _apply_order(files, abs_path, source.get("order") or [])
            parts: list[str] = []
            for f in files:
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = f.relative_to(abs_path)
                parts.append(f"{_FILE_MARKER}{rel}\n{content}")
            if not parts:
                return "", ""
            return f"Workspace — {rel_path}", "\n\n".join(parts)
        return "", ""

    def _resolve_repository(self, source: dict) -> tuple[str, str]:
        """Resolve from: repository source — read from the CODE repository root
        (same root the read-tool branch of this spec uses).

        For mode=tool, returns empty (repos are large, tool-only).
        For mode=inline/both WITH a path, injects that file/subtree.
        For inline WITHOUT a path — refused. That would concatenate the whole
        repo into the prompt (a real repo is megabytes; the one host that hit
        this ballooned a prompt to ~4 MB). It only ever appeared to work
        because the old code read workspace_root/"project", a near-empty dir.
        Use `mode: tool` for a browsable read surface, or name a `path:`.
        """
        if source.get("mode") == "tool":
            return "", ""  # tool-only, no inline injection

        if self._code_root is None:
            # The run declares no code repository, so there is nothing here to
            # read. Reported rather than silently empty: a `from: repository`
            # spec on a repo-less run is a config mistake, and saying so is
            # cheaper to diagnose than an empty injection.
            #
            # No repo-less run reached this branch before, because there was no
            # `False` answer to reach it with: `code_root` came from
            # `get_project_code_path`, which always returned a path. What the
            # fallback above WOULD have done, had `False` been threaded in
            # without this branch, is hand back `workspace_root/"project"` — the
            # populated project BRIEF directory — labelled "Repository". That is
            # a hazard of the new answer, not a history.
            logging.getLogger("skillflow.context").warning(
                "from:repository on a run that declares no code repository — "
                "nothing injected")
            return "", ""

        rel_path = source.get("path", "")
        if not rel_path:
            logging.getLogger("skillflow.context").warning(
                "from:repository with inline mode and no path: refusing to "
                "inject the whole repo into the prompt — use mode: \"tool\" "
                "for read tools, or set path: to a specific file/dir")
            return "", ""
        abs_path = self._code_root / rel_path
        if not abs_path.exists():
            return "", ""
        if abs_path.is_file():
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                return f"Repository — {rel_path}", content
            except Exception:
                return "", ""
        elif abs_path.is_dir():
            files = [f for f in sorted(abs_path.rglob("*"))
                     if f.is_file() and f.name != ".gitkeep"
                     and f.suffix not in (".pyc", ".pyo", ".so", ".o", ".bin")]
            files = _apply_order(files, abs_path, source.get("order") or [])
            if source.get("mode") == "index":
                # Index, not content: the reader learns what exists and how big
                # it is, and reads the files it needs. AItelier's design/ was
                # 368 KB inlined whole into every planner turn (2026-09-02) —
                # two of its files were append-only ledgers nobody reads whole.
                body = _dir_index(files, abs_path)
                if not body:
                    return "", ""
                return f"Repository — {rel_path} (index)", body
            parts: list[str] = []
            for f in files:
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = f.relative_to(abs_path)
                parts.append(f"{_FILE_MARKER}{rel}\n{content}")
            if not parts:
                return "", ""
            return f"Repository — {rel_path}", "\n\n".join(parts)
        return "", ""

    def _resolve_tool(self, source: dict,
                      current_config: str = "") -> tuple[str, str]:
        tool_name = source["tool"]
        if not self._tool_loader:
            return f"[{tool_name}]", ""

        try:
            fn = self._tool_loader.load_fn(tool_name)
            # project_root = the real code repo (same root tool STEPS get),
            # not the workspace's brief dir — a context-source tool like
            # dir_tree must describe the same tree the read tools serve.
            call_kwargs = {
                "workspace_root": str(self._workspace_root),
                # Which pipeline is reading. A context tool that answers a
                # per-pipeline question (what does THIS graph offer?) otherwise
                # gets an empty name and silently answers about everything.
                "config_name": current_config or "",
            }
            # Omitted, not "", when the run declares no code repository. Note
            # what that does and does not buy: a tool whose `project_root` has NO
            # default becomes uncallable (a loud TypeError), while a tool that
            # defaults it to "" cannot tell the two apart — `Path("")` is
            # `Path(".")`, so omitting is byte-identical to passing "" inside the
            # function, and only the tool's own guard keeps it off the process
            # CWD. The reason to omit anyway is that "" is not an answer the
            # engine has; it is the absence of one.
            if self._code_root is not None:
                call_kwargs["project_root"] = str(self._code_root)
            # Add the reading step's capability context (e.g. state_dir), then
            # drop any kwarg the tool doesn't accept (unless it takes **kwargs).
            for _k, _v in self._extra_tool_kwargs.items():
                call_kwargs.setdefault(_k, _v)
            import inspect as _inspect
            _sig = _inspect.signature(fn)
            if not any(_p.kind == _inspect.Parameter.VAR_KEYWORD
                       for _p in _sig.parameters.values()):
                call_kwargs = {k: v for k, v in call_kwargs.items()
                               if k in _sig.parameters}
            result = fn(**call_kwargs)
            if isinstance(result, dict):
                content = result.get("tree", result.get("content", str(result)))
            else:
                content = str(result)
            label = f"[{tool_name}]"
            return label, content
        except Exception:
            return f"[{tool_name}]", ""

    def _project(self, content: str, mode: str, name: str) -> str:
        """Apply a context source's ``mode:`` to ONE file's content.

        Every branch of `_resolve_step_output` routes through here. It used to
        live inline on the named-single-file branch only, so a source that named
        a step but no file — `{step: "2", mode: "interfaces"}` — took the
        "concatenate the whole step dir" branch and returned before `mode` was
        ever read. The projection the config asked for silently did not happen,
        and nothing said so: the reader got the full document under a label that
        promised a slice.
        """
        if mode == "summary":
            # First 100 lines AND at most _SUMMARY_MAX_CHARS: a dense Markdown
            # report (16 KB in 90 lines, jinyong-nicknames 2026-09-03) passed
            # the line rule untouched, so "summary" injected the whole document.
            lines = content.splitlines()
            head = lines[:100]
            text = "\n".join(head)
            if len(text) > _SUMMARY_MAX_CHARS:
                cut = text.rfind("\n", 0, _SUMMARY_MAX_CHARS)
                text = text[:cut if cut > 0 else _SUMMARY_MAX_CHARS]
                return text + "\n... [summary truncated]"
            if len(lines) > 100:
                return text + "\n... [summary truncated]"
            return content
        if mode == "interfaces":
            return self._extract_interfaces(content, name)
        return content

    @staticmethod
    def _dir_listing(entries: list[tuple[str, int, int]]) -> str:
        """Header naming every file in a step dir, with raw → projected sizes.

        A projection that shrinks a document is announced by the projector
        itself; a projection that drops a WHOLE FILE from the reader's view is
        not, because the file simply is not there to carry a marker. That is the
        failure mode of naming one file in a directory of several: the others do
        not get truncated, they get erased, and the reader cannot ask for what it
        cannot see. So a projected directory always says what it contained.
        """
        rows = []
        for name, raw, projected in entries:
            rows.append(f"  - {name} ({raw} B"
                        + (f" → {projected} B after projection)" if projected != raw else ")"))
        return ("[step output contains]\n" + "\n".join(rows)
                + "\nRead any of these directly (read/search) for what a "
                  "projection above left out.\n")

    @staticmethod
    def _extract_interfaces(content: str, source_name: str = "") -> str:
        """Extract API/interface sections from architecture docs.

        LOSSY TWICE OVER — keyword-matched sections only, then a hard byte cap —
        and BOTH losses are announced. A silent extract reads as the whole
        document: an agent handed 8 KB under the heading "the architecture"
        reviews against those 8 KB and passes a design it never saw. `summary`
        mode has always emitted "... [summary truncated]"; this mode returned
        `extracted[:8000]` bare, and on a real 41 KB design doc that dropped 81%
        with nothing to mark it. An unannounced truncation is indistinguishable
        from a short document.

        The marker names the source file, because knowing something is missing
        is only half of it — the reader also needs to know what to open. Every
        step that resolves context this way also carries paged read/search
        tools, so the full file is one call away once the reader knows to make
        it.
        """
        import re
        lines = content.splitlines()
        result: list[str] = []
        interface_keywords = {
            "interface", "api", "contract", "endpoint", "module boundary",
            "component", "data flow", "interaction"
        }
        in_section = False
        section_depth = 0

        for line in lines:
            m = re.match(r'^(#{1,4})\s+(.*)', line)
            if m:
                header_text = m.group(2).lower()
                depth = len(m.group(1))
                if any(kw in header_text for kw in interface_keywords):
                    in_section = True
                    section_depth = depth
                    result.append(line)
                elif in_section and depth <= section_depth:
                    in_section = False
                    if any(kw in header_text for kw in interface_keywords):
                        in_section = True
                        section_depth = depth
                        result.append(line)
                elif in_section:
                    result.append(line)
            elif in_section:
                result.append(line)

        where = f" of {source_name}" if source_name else ""
        total = len(content)

        if not result:
            head = "\n".join(lines[:150])
            return (
                head + f"\n\n... ⚠️ NO INTERFACE SECTIONS FOUND{where} — the above "
                f"is only the FIRST {min(150, len(lines))} of {len(lines)} lines "
                f"({len(head)} of {total} chars). The rest is NOT shown. Read the "
                "file directly (read/search, start_line/end_line) before "
                "concluding anything about what is not above."
            )

        extracted = "\n".join(result)
        if len(extracted) > _INTERFACES_MAX_CHARS:
            return (
                extracted[:_INTERFACES_MAX_CHARS]
                + f"\n\n... ⚠️ TRUNCATED{where} — showing {_INTERFACES_MAX_CHARS} of "
                f"{len(extracted)} extracted chars, themselves an interfaces-only "
                f"slice of a {total}-char document. Non-interface sections were "
                "ALSO dropped. Anything past this point is NOT shown. Read the "
                "file directly (read/search, start_line/end_line) before "
                "concluding it is absent."
            )
        # Compare STRIPPED: the extractor rebuilds with "\n".join(), which loses
        # the source's trailing newline, so a byte-length test calls a document
        # that dropped nothing "truncated". A marker that cries wolf on a
        # complete document teaches the reader to ignore it on a real cut.
        if extracted.strip() != content.strip():
            return (
                extracted
                + f"\n\n... ⚠️ INTERFACES EXTRACT{where} — {len(extracted)} of "
                f"{total} chars. Non-interface sections were dropped. Read the "
                "file directly (read/search) if you need them."
            )
        return extracted
