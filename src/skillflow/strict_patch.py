"""Strict code patches: complete preflight and atomic per-file publication."""
from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from skillflow import citations

MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_BATCH_BYTES = 64 * 1024 * 1024
# Reference hunks are the same unit of work as V4A hunks, addressed
# differently, so they carry the same ceilings.
MAX_REFS_PER_FILE = 1024
MAX_FILES = 128
_REFERENCE_KEYS = frozenset(
    {"file", "sha", "from_line", "from_col", "to_line", "to_col", "new_text"})


class PatchError(ValueError):
    """Malformed, unsafe, stale or ambiguous input; never a fuzzy fallback."""


class UncorroboratedColumns(PatchError):
    """Columns named without a span citation; carries the spans to cite."""

    def __init__(self, message: str, spans: list[dict]):
        super().__init__(message)
        self.spans = spans


@dataclass(frozen=True)
class Hunk:
    """One V4A hunk: the lines it matches and the lines it writes.

    ``rows`` keeps the hunk's own lines in order as ``(prefix, text)``, so the
    applier can say which matched line each deletion removed and between
    which lines each insertion went. A hunk built without rows is recorded as
    one block: all of ``old`` replaced by all of ``new``.
    """
    old: tuple[str, ...]
    new: tuple[str, ...]
    rows: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Reference:
    """One edit addressed by a digest the READ issued, not by copied text.

    The caller supplies `new_text` and nothing else: there is no field here
    that could hold the original, which is the whole point. `sha` is the
    citation the read handed back for the window this range sits in; the
    coordinates are 1-based lines with 0-based character columns, `to_col`
    exclusive, so (from_line, 0) .. (to_line, len(line)) is "replace these
    whole lines" and from == to is an insertion point.

    The columns are OPTIONAL, and omitting them means exactly that whole-line
    span — computed from the line as the read served it rather than counted by
    the caller. That is not sugar. On 2026-09-21 a round
    (attempt-00bbbb91b61e46b184e228c04e94b195) cited a fresh, valid window and
    asked for line 103 columns 17..40 when it meant the whole 58-character
    line. Every check passed, `applied` came back true, and the file was left
    with a six-line comment spliced into the middle of an expression and the
    tail `]ctions() else []),` orphaned below it — a SyntaxError at collection,
    so the round's whole suite scored nothing. Column arithmetic the caller
    does not have to do is column arithmetic it cannot get wrong.

    The lines are OPTIONAL too, and omitting both means the whole window the
    citation was issued for: {file, sha, new_text} replaces exactly the text
    that read served, with every coordinate taken from the citation. This is
    the form that restores the check a copied original used to give. On
    2026-09-21 host run eac7cacb cited the right sha on its first try and asked
    for line 1679 columns 0..27 when the line `DEFAULT_TIMEOUT_SECONDS = 30`
    is 28 characters long; 27 and 28 are both legal columns, so the engine
    wrote `DEFAULT_TIMEOUT_SECONDS = 450` and reported success. A copied
    original is a checksum because a wrong copy fails to match; a counted
    column has no such redundancy. The only check a column can be given
    without retyping the text is a digest of what it covers, and the only
    digests a caller holds are the ones read issued for windows it served, so
    a caller that holds the digest of exactly the text it means to replace can
    cite that window and write no number at all. Here the caller's belief
    about a line's length has no field to travel in: the range written is the
    range read, which the caller was shown and the engine re-checks by sha.
    Explicit lines narrow the window to whole lines.

    Explicit COLUMNS must be corroborated. A reference that names a column
    and cites a window is refused, nothing is written, and the refusal is the
    preview: it shows the exact text those coordinates cover and what they
    leave on either side, and hands back a span citation issued for exactly
    that range (`skillflow.citations.issue_span`). Resending the reference with
    that sha writes; the engine checks the sha names those same coordinates
    and that they still cover the text it showed. A caller that counted 27
    for a 28-character line is shown `DEFAULT_TIMEOUT_SECONDS = 3` with `0`
    left behind before anything is written, and the write it can then make is
    bound to those bytes.
    """
    sha: str
    from_line: int | None
    from_col: int | None
    to_line: int | None
    to_col: int | None
    new_text: str


@dataclass(frozen=True)
class Operation:
    kind: str
    path: str
    hunks: tuple[Hunk, ...] = ()
    content: str = ""
    refs: tuple[Reference, ...] = ()


def patch_path(raw: str) -> str:
    path = PurePosixPath(raw)
    if (not raw or raw in (".", "..") or raw != raw.strip() or raw != path.as_posix()
            or path.is_absolute() or "\\" in raw or ":" in raw
            or any(ord(c) < 32 for c in raw)
            or any(p in (".", "..") or p.casefold() == ".git" for p in path.parts)):
        raise PatchError(f"Unsafe repo-relative patch path: {raw!r}")
    return raw


def parse_patch(patch: str) -> tuple[Operation, ...]:
    """Parse the full batch; also used for the host's scope authorization."""
    if not isinstance(patch, str) or not patch:
        raise PatchError("patch must be a non-empty string")
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise PatchError("patch exceeds 2 MiB; split it into smaller batches")
    text = patch.replace("\r\n", "\n")
    if "\r" in text or "\x00" in text:
        raise PatchError("Patch text must use LF or CRLF lines without NUL")
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise PatchError("Require exact *** Begin Patch and *** End Patch wrappers")
    ops: list[Operation] = []
    seen: set[str] = set()
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        kind = next((k for k in ("Add", "Update", "Delete")
                     if line.startswith(f"*** {k} File: ")), None)
        if kind is None:
            raise PatchError(f"Line {i + 1}: expected Add/Update/Delete File; unsupported syntax")
        name = patch_path(line[len(f"*** {kind} File: "):])
        if name in seen:
            raise PatchError(f"Duplicate operation for {name!r}; combine its hunks")
        seen.add(name)
        i += 1
        body: list[str] = []
        while i < len(lines) - 1 and not lines[i].startswith("*** "):
            body.append(lines[i])
            i += 1
        if kind == "Delete":
            if body:
                raise PatchError(f"{name}: Delete File takes no body")
            ops.append(Operation(kind, name))
        elif kind == "Add":
            if any(not row.startswith("+") for row in body):
                raise PatchError(f"{name}: every Add File content line must start with '+'")
            ops.append(Operation(kind, name, content="".join(row[1:] + "\n" for row in body)))
        else:
            hunks: list[Hunk] = []
            j = 0
            while j < len(body):
                if body[j] != "@@":
                    raise PatchError(f"{name}: require bare @@ headers (no line numbers/anchors)")
                j += 1
                old: list[str] = []
                new: list[str] = []
                rows: list[tuple[str, str]] = []
                changed = False
                while j < len(body) and body[j] != "@@":
                    row = body[j]
                    if not row or row[0] not in " +-":
                        raise PatchError(f"{name}: hunk lines need a space, '-' or '+' prefix")
                    if row[0] in " -":
                        old.append(row[1:])
                    if row[0] in " +":
                        new.append(row[1:])
                    rows.append((row[0], row[1:]))
                    changed |= row[0] != " "
                    j += 1
                if not changed or old == new:
                    raise PatchError(f"{name}: each hunk must change text")
                hunks.append(Hunk(tuple(old), tuple(new), tuple(rows)))
            if not hunks or len(hunks) > 1024:
                raise PatchError(f"{name}: require 1..1024 hunks")
            ops.append(Operation(kind, name, tuple(hunks)))
        if len(ops) > 128:
            raise PatchError("At most 128 files per patch")
    for name in seen:
        if any(parent.as_posix() in seen for parent in PurePosixPath(name).parents):
            raise PatchError(f"File/parent path collision in patch: {name}")
    return tuple(ops)


_REQUIRED_REFERENCE_KEYS = frozenset({"file", "sha", "new_text"})


def _int_field(raw: dict, key: str, index: int) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PatchError(f"reference {index}: {key} must be a non-negative integer")
    return value


def _optional_col(raw: dict, key: str, index: int):
    """A column, or None for "the natural end of the line as it was read"."""
    if raw.get(key) is None:
        return None
    return _int_field(raw, key, index)


def parse_references(references) -> tuple[Operation, ...]:
    """Validate reference hunks and group them per file.

    Order is NOT the caller's problem: references may arrive in any order and
    are sorted against one snapshot at apply time. Requiring descending line
    numbers would move a mechanical discipline back into the prompt, which is
    exactly the kind of rule this mode exists to retire.
    """
    if references in (None, (), []):
        return ()
    if not isinstance(references, list):
        raise PatchError("references must be a list of reference hunks")
    try:
        size = len(json.dumps(references).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise PatchError(f"references must be JSON-serialisable: {exc}") from exc
    if size > MAX_PATCH_BYTES:
        raise PatchError("references exceed 2 MiB; split them into smaller batches")
    grouped: dict[str, list[Reference]] = {}
    for index, raw in enumerate(references, 1):
        if not isinstance(raw, dict):
            raise PatchError(f"reference {index}: expected an object")
        unknown = sorted(set(raw) - _REFERENCE_KEYS)
        if unknown:
            raise PatchError(
                f"reference {index}: unsupported field(s) {unknown}; a reference "
                "hunk carries only the NEW text")
        missing = sorted(_REQUIRED_REFERENCE_KEYS - set(raw))
        if missing:
            raise PatchError(f"reference {index}: missing field(s) {missing}")
        path = raw["file"]
        if not isinstance(path, str):
            raise PatchError(f"reference {index}: file must be a string")
        path = patch_path(path)
        sha = raw["sha"]
        if not isinstance(sha, str) or not sha.strip():
            raise PatchError(f"reference {index}: sha must be the digest the read issued")
        new_text = raw["new_text"]
        if not isinstance(new_text, str):
            raise PatchError(f"reference {index}: new_text must be a string")
        if "\x00" in new_text:
            raise PatchError(f"reference {index}: new_text must not contain NUL")
        lines_given = [raw.get(key) is not None for key in ("from_line", "to_line")]
        if not any(lines_given):
            # The whole cited window: every coordinate comes from the citation.
            if raw.get("from_col") is not None or raw.get("to_col") is not None:
                raise PatchError(
                    f"reference {index}: from_col/to_col need from_line and "
                    "to_line; omit all four to replace the whole cited window")
            from_line = to_line = None
        elif not all(lines_given):
            raise PatchError(
                f"reference {index}: give both from_line and to_line, or "
                "neither to replace the whole cited window")
        else:
            from_line = _int_field(raw, "from_line", index)
            to_line = _int_field(raw, "to_line", index)
            if from_line < 1 or to_line < from_line:
                raise PatchError(
                    f"reference {index}: require 1 <= from_line <= to_line")
        ref = Reference(sha.strip(), from_line,
                        _optional_col(raw, "from_col", index),
                        to_line, _optional_col(raw, "to_col", index), new_text)
        bucket = grouped.setdefault(path, [])
        if len(bucket) >= MAX_REFS_PER_FILE:
            raise PatchError(f"{path}: at most {MAX_REFS_PER_FILE} reference hunks per file")
        bucket.append(ref)
    if len(grouped) > MAX_FILES:
        raise PatchError(f"At most {MAX_FILES} files per patch")
    return tuple(Operation("Cite", path, refs=tuple(refs))
                 for path, refs in grouped.items())


def _framed(before: bytes, path: str) -> tuple[list[str], str, bool]:
    """The file as normalised lines plus how to write them back."""
    try:
        text = before.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"{path}: editing requires UTF-8 text") from exc
    if "\x00" in text:
        raise PatchError(f"{path}: binary/NUL content is not editable")
    eol = "\r\n" if "\r\n" in text else "\n"
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized or (eol == "\r\n" and "\n" in text.replace("\r\n", "")):
        raise PatchError(f"{path}: mixed or lone-CR line endings are not supported")
    final_newline = not text or text.endswith("\n")
    original = normalized.split("\n") if text else []
    if text.endswith("\n"):
        original.pop()
    return original, eol, final_newline


def _window_offset(op: Operation, number: int, record: dict, line_no: int,
                   col: int, which: str) -> int:
    """Where (line, col) sits inside the window the read actually served.

    Resolving against the SERVED text rather than the current file is the whole
    of the fix for `to column N is past the end of line M`: a column that was
    valid in the text the caller was shown stays valid however the rest of the
    file moves, and a column that was never valid still fails — naming the file
    and the line, and saying the length is the length AS READ so the caller can
    tell the two apart.
    """
    lines = record["text"].split("\n")
    index = line_no - record["start_line"]
    line = lines[index]
    if col is None:
        col = 0 if which == "from" else len(line)
    if col > len(line):
        raise PatchError(
            f"{op.path} reference {number}: {which} column {col} is past the "
            f"end of line {line_no} as the read served it ({len(line)} "
            "characters); cite a column inside the line you read")
    return sum(len(lines[k]) + 1 for k in range(index)) + col


MAX_ECHO_CHARS = 240


def _coords(from_line, from_col, to_line, to_col) -> str:
    def col(value):
        return "-" if value is None else str(value)
    return f"{from_line}:{col(from_col)}..{to_line}:{col(to_col)}"


def _shown(text: str) -> str:
    """The span as shown to the caller: whole, or both ends when it is long.

    A miscounted column moves an END of the span, so the ends are what must
    never be cut from the preview.
    """
    if len(text) <= 2 * MAX_ECHO_CHARS:
        return text
    return text[:MAX_ECHO_CHARS] + "…" + text[-MAX_ECHO_CHARS:]


def _span_range(op: Operation, number: int, ref: Reference, record: dict,
                run_id: str, joined: str, current_sha: str) -> tuple[int, int]:
    """Where a span citation's text sits now, or a refusal.

    The reference must name the span's own coordinates (or none, meaning the
    span), and those coordinates must still cover exactly the text the engine
    showed when it issued the span. The text is located through the same
    journal a window uses, so this run's own edit elsewhere in the file does
    not stale it. Two checks refuse everything else: the chain check (the file
    was written by something other than this run's journaled edits since the
    span was shown) and the text check (this run's own edit changed the
    shown text itself).
    """
    span = (record["start_line"], record["from_col"], record["end_line"],
            record["to_col"])
    named = (ref.from_line, ref.from_col, ref.to_line, ref.to_col)
    if ref.from_line is not None and named != span:
        raise PatchError(
            f"{op.path} reference {number}: that sha is the span "
            f"{_coords(*span)} and this reference names {_coords(*named)}; a "
            "span citation corroborates only the coordinates it was issued for")
    stale = PatchError(
        f"{op.path} reference {number}: span {_coords(*span)} changed since "
        "its citation was issued: the text it showed was edited, or the file "
        "was written by something other than this run's apply_patch; reread "
        "the range and resend the reference with the new digest to be shown "
        "the text it covers now")
    if not citations.chain_intact(run_id, op.path, record.get("generation"),
                                  record.get("file_sha", ""), current_sha,
                                  epoch=record.get("epoch")):
        raise stale
    placed = citations.translate(run_id, op.path, record["generation"],
                                 record["start_char"],
                                 record["start_char"] + len(record["text"]))
    if placed == citations.POINT:
        raise PatchError(
            f"{op.path} reference {number}: span {_coords(*span)} is an "
            "insertion point and an earlier edit in this run inserted text "
            "exactly there, so which side of that text it meant cannot be "
            "told; reread the range and resend the reference with the new "
            "digest to be shown where it lands now")
    start, end = placed if isinstance(placed, tuple) else (None, None)
    if start is None or end is None or joined[start:end] != record["text"]:
        raise stale
    return start, end


def _refuse_uncorroborated(op: Operation, pending: list, run_id: str,
                           joined: str, current_sha: str) -> None:
    """Refuse counted columns, showing what they cover and a sha for exactly it.

    This refusal is the preview. It writes nothing; it shows the exact text
    the named coordinates cover and what they leave on either side of it, and
    issues a span citation for exactly those coordinates over exactly that
    text. The caller that meant it resends the same reference with that sha.
    """
    spans: list[dict] = []
    lines: list[str] = []
    for start, end, _new, from_line, to_line, (number, ref, record), _ in pending:
        text = joined[start:end]
        line_start = joined.rfind("\n", 0, start) + 1
        line_end = joined.find("\n", end)
        if line_end < 0:
            line_end = len(joined)
        coords = (from_line, ref.from_col, to_line, ref.to_col)
        issued = citations.issue_span(
            run_id, path=op.path, source=record.get("source", ""),
            from_line=from_line, from_col=ref.from_col, to_line=to_line,
            to_col=ref.to_col, text=text, start_char=start,
            file_sha=current_sha)
        keeps_before = joined[line_start:start]
        keeps_after = joined[end:line_end]
        spans.append({
            "file": op.path, "reference": number,
            "from_line": from_line, "from_col": ref.from_col,
            "to_line": to_line, "to_col": ref.to_col,
            "sha": issued["sha"], "chars": len(text), "text": _shown(text),
            "keeps_before": _shown(keeps_before), "keeps_after": _shown(keeps_after),
        })
        lines.append(
            f"reference {number} names columns {_coords(*coords)}, which cover "
            f"{_shown(text)!r} and leave {_shown(keeps_before)!r} before it and "
            f"{_shown(keeps_after)!r} after it on their lines; if that is exactly "
            f"the text to replace, resend this reference with sha "
            f"{issued['sha']} (in `spans`)")
    raise UncorroboratedColumns(
        f"{op.path}: a column the caller counted has nothing to check it "
        "against, so columns must cite a span citation issued for exactly "
        "those coordinates. Nothing was written. " + "; ".join(lines)
        + ". To replace whole lines instead, omit from_col and to_col.",
        spans)


def _recorded(terminated: str, start: int, end: int, new_text: str,
              whole_lines: bool) -> list[tuple[int, int, int]]:
    """One reference's write as the journal records it: spans of
    ``[start, end)`` removed and ``new_length`` characters written.

    ``terminated`` is the text the write was applied to, every line ending in
    its newline. A write of whole lines replaces them WITH their newlines, so
    a blank line replaced by text is recorded as its newline removed and the
    text plus a newline written, never as a pure insertion.

    A column write that starts at a line's end, where the text it removes (or
    writes) begins with that newline and it stops at another line's end, is a
    whole-line edit: ``"\\n" + text`` after a line, or lines deleted from the
    end of the line above. Inserting ``"\\n" + text`` before a newline and
    ``text + "\\n"`` after it are one change, so it is recorded where a V4A
    hunk records the same change, one character later, at the next line's
    start; the same change then journals the same in every format.

    When the line it starts at is blank, that position is also a line start,
    and when the text it removes and the text it writes each end with a
    newline (or are empty), the call reads as a whole-line edit on either side
    of the blank line: the same coordinates and text are what a caller sends
    to write before that line and to write after it. Which one was meant
    cannot be told, so the blank line is recorded as rewritten, and a pure
    insertion is recorded after it as well; every older citation that the two
    readings would place differently then meets a removed range or an
    insertion offset and is refused.
    """
    if whole_lines:
        return [(start, end + 1, len(new_text) + 1)]
    size = len(terminated)
    if not (start < size and terminated[start] == "\n"
            and end < size and terminated[end] == "\n"
            and (new_text[:1] == "\n" or (not new_text and end > start))):
        return [(start, end, len(new_text))]
    blank = start == 0 or terminated[start - 1] == "\n"
    removed = terminated[start:end]
    if (blank and (not removed or removed.endswith("\n"))
            and (not new_text or new_text.endswith("\n"))):
        if not removed:
            return [(start, start + 1, 1),
                    (start + 1, start + 1, len(new_text))]
        return [(start, end + 1, len(new_text) + 1)]
    return [(start + 1, end + 1, len(new_text))]


def cited_bytes(before: bytes, op: Operation, run_id: str,
                edits: list | None = None, echo: list | None = None) -> bytes:
    """Apply this file's reference hunks against ONE snapshot of its text.

    Every range is resolved against the same `original`, so the caller never
    compensates for line drift inside a batch and never has to order its
    edits; the engine sorts them and refuses any overlap. A citation that this
    run did not issue, or whose range no longer reads the same, is refused
    outright — there is no fuzzy match and no partial write.

    Coordinates are the CITATION's, not the current file's: they are resolved
    inside the text that read served and then translated through the edits this
    run published for the file (`skillflow.citations`). So a successful write
    does not cost the caller a reread before the next one — which is what a
    round measured at 54 reads and 37 writes on one file was paying for. The
    translation is refused rather than guessed when the cited range was itself
    replaced, and the chain is only trusted while the file is still what the
    engine left there; otherwise the citation is refused. Only a citation
    issued without a frame is judged by the original strict window check.

    ``edits`` is filled, when given, with the `_recorded` spans of each
    reference this call applied, in the offsets of the text it applied them
    to with every line ending in its newline — that is what the journal
    records. ``echo`` is filled with what each reference actually replaced:
    a legal span is not necessarily the intended one, and the only way that
    was ever visible was to read the file back.
    """
    original, eol, final_newline = _framed(before, op.path)
    joined = "\n".join(original)
    terminated = joined + "\n" if original else ""
    current_sha = citations.text_sha(joined)
    starts: list[int] = []
    run = 0
    for line in original:
        starts.append(run)
        run += len(line) + 1

    def _offset(line_no: int, col: int, which: str) -> int:
        if line_no > len(original):
            raise PatchError(
                f"{op.path}: {which} line {line_no} is past the end of the file "
                f"({len(original)} lines); reread the range")
        line = original[line_no - 1]
        if col is None:
            col = 0 if which == "from" else len(line)
        if col > len(line):
            raise PatchError(
                f"{op.path}: {which} column {col} is past the end of line "
                f"{line_no} ({len(line)} characters); reread the range")
        return starts[line_no - 1] + col

    resolved: list[tuple] = []
    for number, ref in enumerate(op.refs, 1):
        record = citations.lookup(run_id, ref.sha)
        if record is None:
            raise PatchError(
                f"{op.path} reference {number}: sha {ref.sha[:12]}… was never issued "
                "by a read in this run; cite a digest a read handed you")
        if record["path"] != op.path:
            raise PatchError(
                f"{op.path} reference {number}: that digest was issued for "
                f"{record['path']!r}")
        if record.get("kind") == "span":
            start, end = _span_range(op, number, ref, record, run_id, joined,
                                     current_sha)
            resolved.append((start, end, ref.new_text.replace("\r\n", "\n"),
                             record["start_line"], record["end_line"], None,
                             False))
            continue
        if ref.from_line is None:
            from_line, to_line = record["start_line"], record["end_line"]
        else:
            from_line, to_line = ref.from_line, ref.to_line
        if from_line < record["start_line"] or to_line > record["end_line"]:
            raise PatchError(
                f"{op.path} reference {number}: lines {from_line}-{to_line} "
                f"fall outside the cited window {record['start_line']}-"
                f"{record['end_line']}; cite the window that contains them")
        whole_lines = ref.from_col is None and ref.to_col is None
        generation = record.get("generation")
        window_start = record.get("start_char")
        translatable = (
            window_start is not None
            and citations.chain_intact(run_id, op.path, generation,
                                       record.get("file_sha", ""), current_sha,
                                       epoch=record.get("epoch")))
        if translatable:
            local_from = _window_offset(op, number, record, from_line,
                                        ref.from_col, "from")
            local_to = _window_offset(op, number, record, to_line,
                                      ref.to_col, "to")
            # Whole lines own their newlines, so a blank line is one
            # character wide and a range of lines is never empty.
            own = 1 if whole_lines else 0
            placed = citations.translate(run_id, op.path, generation,
                                         window_start + local_from,
                                         window_start + local_to + own)
            if placed == citations.REMOVED:
                raise PatchError(
                    f"{op.path} reference {number}: lines {from_line}-"
                    f"{to_line} changed since the digest was issued: they were "
                    "removed or replaced by an earlier edit in this run; "
                    "reread the range and cite the new digest")
            if placed == citations.SPLIT:
                raise PatchError(
                    f"{op.path} reference {number}: lines {from_line}-"
                    f"{to_line} changed since the digest was issued: an "
                    "earlier edit in this run inserted text inside them; "
                    "reread the range and cite the new digest")
            if placed == citations.POINT:
                raise PatchError(
                    f"{op.path} reference {number}: columns {ref.from_col}.."
                    f"{ref.to_col} are an insertion point and an earlier edit "
                    "in this run inserted text exactly there, so which side "
                    "of that text they meant cannot be told; reread the range "
                    "and cite the new digest")
            start, end = placed[0], placed[1] - own
            if joined[start:end] != record["text"][local_from:local_to]:
                raise PatchError(
                    f"{op.path} reference {number}: lines {from_line}-"
                    f"{to_line} changed since the digest was issued; "
                    "reread the range and cite the new digest")
        else:
            if window_start is not None:
                # The read placed this window in a frame the journal can no
                # longer connect to the file as it is now. Lines that still
                # READ the same may be different lines: a file of repeated
                # lines with one written in above looks unchanged at every
                # line number.
                raise PatchError(
                    f"{op.path} reference {number}: lines {record['start_line']}-"
                    f"{record['end_line']} changed since the digest was issued, "
                    "by a write this run's journal does not account for (a "
                    "write from outside the run, or history the engine no "
                    "longer holds), so where they are now is unknown; reread "
                    "the range and cite the new digest")
            window = "\n".join(original[record["start_line"] - 1:record["end_line"]])
            if not citations.matches(record, run_id, window):
                raise PatchError(
                    f"{op.path} reference {number}: lines {record['start_line']}-"
                    f"{record['end_line']} changed since the digest was issued; "
                    "reread the range and cite the new digest")
            start = _offset(from_line, ref.from_col, "from")
            end = _offset(to_line, ref.to_col, "to")
        if end < start:
            raise PatchError(f"{op.path} reference {number}: end precedes start")
        # A column the caller counted and nothing corroborates. Resolved all
        # the same, so every other refusal (outside the window, past the end
        # of the line, stale, overlapping) keeps its own message and comes
        # first; this one is raised only for a batch that is otherwise valid.
        uncorroborated = ((number, ref, record)
                          if ref.from_col is not None or ref.to_col is not None
                          else None)
        resolved.append((start, end, ref.new_text.replace("\r\n", "\n"),
                         from_line, to_line, uncorroborated, whole_lines))
    resolved.sort(key=lambda item: (item[0], item[1]))
    cursor = 0
    for number, item in enumerate(resolved, 1):
        if item[0] < cursor:
            raise PatchError(
                f"{op.path} reference {number}: overlaps an earlier reference; "
                "cite disjoint ranges")
        cursor = item[1]
    pending = [item for item in resolved if item[5] is not None]
    if pending:
        _refuse_uncorroborated(op, pending, run_id, joined, current_sha)
    cursor = 0
    pieces: list[str] = []
    for start, end, new_text, from_line, to_line, _, whole in resolved:
        pieces.append(joined[cursor:start])
        pieces.append(new_text)
        cursor = end
        if edits is not None:
            edits.extend(_recorded(terminated, start, end, new_text, whole))
        if echo is not None:
            was = joined[start:end]
            said = {
                "file": op.path,
                "from_line": from_line, "to_line": to_line,
                "replaced_chars": len(was),
                "replaced": (was if len(was) <= MAX_ECHO_CHARS
                             else was[:MAX_ECHO_CHARS] + "…"),
            }
            lines_was = was.count("\n") + 1 if was else 0
            lines_new = new_text.count("\n") + 1 if new_text else 0
            if lines_was != lines_new:
                # A change in the number of lines is the one thing the echo
                # above cuts off: a window of ten lines replaced by one shows
                # only its first characters. Bytes are the file's own. The two
                # counts count line PIECES, 1 + the newlines in a non-empty
                # text and 0 for an empty one, not whole lines: a whole-line
                # insertion `x\n` at a line start reports 0 -> 2, one line
                # deleted with its newline 2 -> 0.
                said["replaced_lines"] = lines_was
                said["replaced_bytes"] = len(was.replace(
                    "\n", eol).encode("utf-8"))
                said["new_lines"] = lines_new
            echo.append(said)
    pieces.append(joined[cursor:])
    result = "".join(pieces).split("\n")
    return (eol.join(result) + (eol if result and final_newline else "")).encode("utf-8")


def _line_blocks(placed) -> list[tuple[int, int, tuple[str, ...]]]:
    """``(first old line, lines deleted, lines inserted)`` per changed block.

    ``placed`` is each hunk with the line the applier matched it at. The
    blocks are read off the hunk's own rows: a context row is a line kept, a
    `-` row the matched line it removed, a `+` row a line written before the
    next kept one. Neighbouring changed rows form one block, across a hunk
    boundary too when no kept line separates them.
    """
    blocks: list[list] = []
    open_block = None
    cursor = 0
    for hunk, start in placed:
        if start != cursor:
            open_block = None
        rows = hunk.rows or (tuple(("-", t) for t in hunk.old)
                             + tuple(("+", t) for t in hunk.new))
        index = start
        for sign, text in rows:
            if sign == " ":
                index += 1
                open_block = None
                continue
            if open_block is None:
                open_block = [index, 0, []]
                blocks.append(open_block)
            if sign == "-":
                open_block[1] += 1
                index += 1
            else:
                open_block[2].append(text)
        cursor = start + len(hunk.old)
    return [(k, m, tuple(new)) for k, m, new in blocks]


def _block_spans(original: list[str], blocks) -> list[tuple[int, int, int]]:
    """The blocks as ``(start, end, new_length)`` in the old text with every
    line ending in its newline.

    Lines k..k+m-1 are removed with their newlines (``[start of line k,
    start of line k+m)``) and the new lines are written there, each with its
    newline. A block that removes nothing is a pure insertion at the start of
    line k; a block that removes a blank line removes its newline.
    """
    starts = []
    run = 0
    for line in original:
        starts.append(run)
        run += len(line) + 1
    starts.append(run)
    return [(starts[k], starts[k + m], sum(len(line) + 1 for line in new))
            for k, m, new in blocks]


def updated_bytes(before: bytes, op: Operation, edits: list | None = None) -> bytes:
    """Apply V4A hunks; fill ``edits`` with the spans the applier wrote.

    ``edits`` gets one ``(start, end, new_length)`` per changed block, in the
    offsets of the old text as the journal frames it, taken from where each
    hunk matched and from its own rows. Not recomputed from the two texts
    afterwards: next to repeated lines those have more than one reading.
    """
    try:
        text = before.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"{op.path}: Update File requires UTF-8 text") from exc
    if "\x00" in text:
        raise PatchError(f"{op.path}: binary/NUL content is not editable")
    eol = "\r\n" if "\r\n" in text else "\n"
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized or (eol == "\r\n" and "\n" in text.replace("\r\n", "")):
        raise PatchError(f"{op.path}: mixed or lone-CR line endings are not supported")
    final_newline = not text or text.endswith("\n")
    original = normalized.split("\n") if text else []
    if text.endswith("\n"):
        original.pop()
    framed = "\n" + "\n".join(original) + "\n" if original else ""
    result: list[str] = []
    placed: list[tuple[Hunk, int]] = []
    cursor = 0
    for number, hunk in enumerate(op.hunks, 1):
        if not hunk.old:
            if original or len(op.hunks) != 1:
                raise PatchError(f"{op.path} hunk {number}: insertion needs unchanged context")
            positions = [0]
        else:
            # Framing by newlines enforces whole-line matching, including
            # blank lines and EOF. str.find avoids slicing a large hunk at
            # every candidate line; a second exact match is always refused.
            needle = "\n" + "\n".join(hunk.old) + "\n"
            first = framed.find(needle)
            positions = [] if first < 0 else [framed.count("\n", 0, first)]
            if first >= 0 and framed.find(needle, first + 1) >= 0:
                positions.append(-1)  # ambiguity, never used as a location
        if not positions:
            raise PatchError(
                f"{op.path} hunk {number}: stale (no exact match); "
                "reread that range and cite its sha in references — do not "
                "retype the original"
            )
        if len(positions) != 1:
            raise PatchError(
                f"{op.path} hunk {number}: ambiguous (multiple exact matches); "
                "cite the range you mean in references instead of widening "
                "the copied context"
            )
        start = positions[0]
        if start < cursor:
            raise PatchError(f"{op.path}: hunks overlap or are out of order; combine them")
        result.extend(original[cursor:start])
        result.extend(hunk.new)
        placed.append((hunk, start))
        cursor = start + len(hunk.old)
    result.extend(original[cursor:])
    if edits is not None:
        edits.extend(_block_spans(original, _line_blocks(placed)))
    return (eol.join(result) + (eol if result and final_newline else "")).encode("utf-8")


def _normalised(data: bytes) -> str | None:
    """The file's text the way a read frames it, or None if it is not text."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in text:
        return None
    lines = text.replace("\r\n", "\n").split("\n") if text else []
    if text.endswith("\n"):
        lines.pop()
    return "\n".join(lines)


def _terminated(text: str, data: bytes) -> str:
    """``text`` (`_normalised` ``data``) with every line ending in a newline,
    the last one too: the offsets the journal records in."""
    return text + "\n" if data else ""


def _spans_account_for(old: str, new: str, spans) -> bool:
    """True when ``spans`` turn ``old`` into ``new`` outside the spans' own text.

    Every character outside a span must reappear, in order, exactly where the
    spans say it moved to; that is what `citations.translate` will assume. It
    cannot say WHICH of two valid readings an edit was — that comes from the
    applier — only that the recorded one is a reading of this edit at all.
    """
    position = 0
    cursor = 0
    for start, end, new_length in sorted(spans):
        if start < cursor or end < start or end > len(old):
            return False
        kept = old[cursor:start]
        if new[position:position + len(kept)] != kept:
            return False
        position += len(kept) + new_length
        cursor = end
    return new[position:] == old[cursor:]


@dataclass(frozen=True)
class Snapshot:
    data: bytes
    identity: tuple[int, ...]
    mode: int


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)


@contextmanager
def _parent(root_fd: int, name: str):
    """Open every parent component without following symlinks; pin the parent fd."""
    fd = os.dup(root_fd)
    try:
        for part in PurePosixPath(name).parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


def _read_at(parent_fd: int, name: str) -> Snapshot | None:
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise PatchError(f"{name}: expected a regular file, not a symlink/directory/special file")
    if st.st_size > MAX_FILE_BYTES:
        raise PatchError(f"{name}: file exceeds the 16 MiB limit")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if _identity(opened) != _identity(st):
            raise PatchError(f"{name}: file changed while being opened")
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_FILE_BYTES or _identity(after) != _identity(st):
        raise PatchError(f"{name}: file changed during preflight")
    return Snapshot(data, _identity(st), stat.S_IMODE(st.st_mode))


def _snapshot(root_fd: int, name: str) -> Snapshot | None:
    try:
        with _parent(root_fd, name) as parent_fd:
            return _read_at(parent_fd, PurePosixPath(name).name)
    except FileNotFoundError:
        return None


def _journal(run_id: str, op: Operation, before, after: bytes,
             spans: list[tuple[int, int, int]]) -> None:
    """Tell the citation journal what this publication did to ``op.path``.

    The one recorder for every edit format. ``spans`` are what the applier
    that made this edit wrote — `cited_bytes` for references, spans and
    windows, `updated_bytes` for V4A hunks — never a diff of the two texts
    taken afterwards: next to repeated lines a diff has two readings, and
    the wrong one moves older citations onto the wrong copy.

    Offsets are those of the text with every line ending in its newline
    (`_terminated`), so a removed blank line is a removed character.

    Recorded AFTER the write, so a citation is never translated through an
    edit that did not land. Anything the engine cannot describe as spans
    (an Add, a text it cannot frame, spans that do not account for the
    change) breaks the chain instead of being guessed at: a broken chain
    costs a reread, a wrong span costs a corrupted file.
    """
    old = _normalised(before.data) if before is not None else None
    new = _normalised(after)
    if old is None or new is None or not _spans_account_for(
            _terminated(old, before.data), _terminated(new, after), spans):
        citations.break_journal(run_id, op.path)
        return
    citations.journal_edit(run_id, op.path, edits=spans,
                           sha_before=citations.text_sha(old),
                           sha_after=citations.text_sha(new))


def apply_code_patch(patch: str, root: Path, references=None,
                     run_id: str = "") -> dict:
    """Preflight a patch, then use the existing direct-code mutation backend.

    Validation and matching failures are atomic for the complete batch: all
    operations are preflighted before publication. Publication is atomic per file,
    not per batch. On an I/O error, report the actual completed operations for
    SkillFlow's code journal and agent recovery.
    """
    from skillflow.output_targets import code_path
    from skillflow.write_tools import _write_output_text

    changed: list[str] = []
    deleted: list[str] = []
    replaced: list[dict] = []
    phase = "preflight"
    root_fd = None
    try:
        ops = (parse_patch(patch) if patch else ()) + parse_references(references)
        if not ops:
            raise PatchError("nothing to apply: pass a patch, references, or both")
        seen: set[str] = set()
        for op in ops:
            if op.path in seen:
                raise PatchError(f"Duplicate operation for {op.path!r}; combine its hunks")
            seen.add(op.path)
        if len(ops) > MAX_FILES:
            raise PatchError(f"At most {MAX_FILES} files per patch")
        if not root.is_absolute() or not root.is_dir():
            raise PatchError("Require an existing injected absolute code root")
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        prepared = []
        size = 0
        for op in ops:
            before = _snapshot(root_fd, op.path)
            spans: list[tuple[int, int, int]] = []
            echo: list[dict] = []
            if op.kind == "Add":
                if before is not None:
                    raise PatchError(f"{op.path}: Add File refuses an existing file")
                after = op.content.encode("utf-8")
            else:
                if before is None:
                    raise PatchError(f"{op.path}: {op.kind} File requires an existing file")
                if op.kind == "Update":
                    after = updated_bytes(before.data, op, edits=spans)
                elif op.kind == "Cite":
                    after = cited_bytes(before.data, op, run_id, edits=spans,
                                        echo=echo)
                else:
                    after = None
            if after is not None and len(after) > MAX_FILE_BYTES:
                raise PatchError(f"{op.path}: updated file exceeds 16 MiB")
            size += (len(before.data) if before else 0) + (len(after) if after else 0)
            if size > MAX_BATCH_BYTES:
                raise PatchError("Batch exceeds the 64 MiB preflight content budget")
            prepared.append((op, before, after, spans, echo))
        for op, before, _, _, _ in prepared:
            if _snapshot(root_fd, op.path) != before:
                raise PatchError(f"{op.path}: changed since preflight")
        phase = "publish"
        for op, before, after, spans, echo in prepared:
            if _snapshot(root_fd, op.path) != before:
                raise PatchError(f"{op.path}: changed before publication")
            target = code_path(root, op.path)
            if op.kind == "Delete":
                # Unlink relative to the pinned, symlink-free parent descriptor.
                # The immediately preceding snapshot check proves this directory
                # entry is still the regular file preflight inspected.
                with _parent(root_fd, op.path) as parent_fd:
                    os.unlink(PurePosixPath(op.path).name, dir_fd=parent_fd)
                deleted.append(op.path)
                citations.break_journal(run_id, op.path)
            else:
                _write_output_text(target, after.decode("utf-8"), direct=True, newline="")
                changed.append(op.path)
                replaced.extend(echo)
                _journal(run_id, op, before, after, spans)
        result = {"written": changed, "deleted": deleted, "applied": True,
                  "output_target": "code"}
        if replaced:
            # What the engine took out, said back. A span can be legal and
            # still be the wrong one; before this, the only way to find that
            # out was to read the file again, which is the cost this whole
            # mode exists to remove.
            result["replaced"] = replaced
        return result
    except (PatchError, OSError, UnicodeError) as exc:
        refused = {"error": f"apply_patch {phase}: {exc}", "phase": phase,
                   "written": changed, "deleted": deleted, "applied": False,
                   "partial": bool(changed or deleted), "output_target": "code"}
        if isinstance(exc, UncorroboratedColumns):
            refused["spans"] = exc.spans
        return refused
    finally:
        if root_fd is not None:
            os.close(root_fd)
