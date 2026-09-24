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


@dataclass(frozen=True)
class Hunk:
    old: tuple[str, ...]
    new: tuple[str, ...]


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
    Explicit lines narrow the window to whole lines, and explicit columns cut
    inside a line; both are honoured exactly as stated.
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
                changed = False
                while j < len(body) and body[j] != "@@":
                    row = body[j]
                    if not row or row[0] not in " +-":
                        raise PatchError(f"{name}: hunk lines need a space, '-' or '+' prefix")
                    if row[0] in " -":
                        old.append(row[1:])
                    if row[0] in " +":
                        new.append(row[1:])
                    changed |= row[0] != " "
                    j += 1
                if not changed or old == new:
                    raise PatchError(f"{name}: each hunk must change text")
                hunks.append(Hunk(tuple(old), tuple(new)))
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
    engine left there; otherwise the original strict window check decides.

    ``edits`` is filled, when given, with the disjoint
    ``(start, end, new_length)`` spans this call applied, in the offsets of the
    text it applied them to — that is what the journal records. ``echo`` is
    filled with what each reference actually replaced: a legal span is not
    necessarily the intended one, and the only way that was ever visible was
    to read the file back.
    """
    original, eol, final_newline = _framed(before, op.path)
    joined = "\n".join(original)
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

    resolved: list[tuple[int, int, str, Reference]] = []
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
        if ref.from_line is None:
            from_line, to_line = record["start_line"], record["end_line"]
        else:
            from_line, to_line = ref.from_line, ref.to_line
        if from_line < record["start_line"] or to_line > record["end_line"]:
            raise PatchError(
                f"{op.path} reference {number}: lines {from_line}-{to_line} "
                f"fall outside the cited window {record['start_line']}-"
                f"{record['end_line']}; cite the window that contains them")
        generation = record.get("generation")
        window_start = record.get("start_char")
        translatable = (
            window_start is not None
            and citations.chain_intact(run_id, op.path, generation,
                                       record.get("file_sha", ""), current_sha))
        if translatable:
            local_from = _window_offset(op, number, record, from_line,
                                        ref.from_col, "from")
            local_to = _window_offset(op, number, record, to_line,
                                      ref.to_col, "to")
            start = citations.remap(run_id, op.path, generation,
                                    window_start + local_from)
            end = citations.remap(run_id, op.path, generation,
                                  window_start + local_to)
            if start is None or end is None:
                raise PatchError(
                    f"{op.path} reference {number}: lines {from_line}-"
                    f"{to_line} were themselves replaced by an earlier "
                    "edit in this run; reread that range and cite the new digest")
            if joined[start:end] != record["text"][local_from:local_to]:
                raise PatchError(
                    f"{op.path} reference {number}: lines {from_line}-"
                    f"{to_line} changed since the digest was issued; "
                    "reread the range and cite the new digest")
        else:
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
        resolved.append((start, end, ref.new_text.replace("\r\n", "\n"),
                         from_line, to_line))
    resolved.sort(key=lambda item: (item[0], item[1]))
    cursor = 0
    pieces: list[str] = []
    for number, (start, end, new_text, from_line, to_line) in enumerate(resolved, 1):
        if start < cursor:
            raise PatchError(
                f"{op.path} reference {number}: overlaps an earlier reference; "
                "cite disjoint ranges")
        pieces.append(joined[cursor:start])
        pieces.append(new_text)
        cursor = end
        if edits is not None:
            edits.append((start, end, len(new_text)))
        if echo is not None:
            was = joined[start:end]
            echo.append({
                "file": op.path,
                "from_line": from_line, "to_line": to_line,
                "replaced_chars": len(was),
                "replaced": (was if len(was) <= MAX_ECHO_CHARS
                             else was[:MAX_ECHO_CHARS] + "…"),
            })
    pieces.append(joined[cursor:])
    result = "".join(pieces).split("\n")
    return (eol.join(result) + (eol if result and final_newline else "")).encode("utf-8")


def updated_bytes(before: bytes, op: Operation) -> bytes:
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
        cursor = start + len(hunk.old)
    result.extend(original[cursor:])
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


def _collapsed_edit(before: str, after: str) -> list[tuple[int, int, int]]:
    """One span covering everything that differs, in ``before``'s offsets.

    For a V4A hunk batch the engine knows the two texts but not a per-hunk
    span it could state in characters (a whole-line deletion is not a
    character range), so it states the truth it does have: the region between
    the first and last difference changed. Coordinates outside it translate;
    coordinates inside it are refused, which is correct — they may be anywhere.
    """
    if before == after:
        return []
    head = 0
    limit = min(len(before), len(after))
    while head < limit and before[head] == after[head]:
        head += 1
    tail = 0
    while (tail < limit - head
           and before[len(before) - 1 - tail] == after[len(after) - 1 - tail]):
        tail += 1
    return [(head, len(before) - tail, len(after) - tail - head)]


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

    Recorded AFTER the write, so a citation is never translated through an
    edit that did not land. Anything the engine cannot describe as spans
    (an Add, a text it cannot frame) breaks the chain instead of being guessed
    at: a broken chain costs a reread, a wrong span costs a corrupted file.
    """
    old = _normalised(before.data) if before is not None else None
    new = _normalised(after)
    if old is None or new is None:
        citations.break_journal(run_id, op.path)
        return
    if op.kind != "Cite":
        spans = _collapsed_edit(old, new)
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
                    after = updated_bytes(before.data, op)
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
        return {"error": f"apply_patch {phase}: {exc}", "phase": phase,
                "written": changed, "deleted": deleted, "applied": False,
                "partial": bool(changed or deleted), "output_target": "code"}
    finally:
        if root_fd is not None:
            os.close(root_fd)
