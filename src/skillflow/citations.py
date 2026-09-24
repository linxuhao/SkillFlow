"""Digests the engine issues for the text a read actually sent.

An editor that requires the caller to reproduce the original text makes
"is my context still accurate?" a question with no machine answer: the only
way to find out is to read again. A measured coding step spent 13 reads, 8
searches and 2 semantic searches producing zero writes, on a 44,602-character
file it could never hold in one 24,000-character read window.

A citation replaces that question with a precondition the engine checks. The
read reports the exact range it served and a digest OF that range; the caller
quotes the digest back and supplies only the NEW text. Two properties make the
digest worth trusting, and both live here rather than in a document:

* The caller cannot compute one. The digest is keyed with a secret generated
  once per process and never emitted, so a caller holding the original text
  still cannot produce a digest the engine will accept. If it could, a caller
  could cite a range it had never read — blind coordinate editing, which is
  strictly more dangerous than requiring the text.
* It must have been issued. Every digest is recorded per run, so a value that
  is arithmetically correct but was never served in THIS run is refused.

Eviction is fail-closed: a forgotten citation is refused, and the remedy is to
read the range again.

## The coordinate frame is frozen at issue time

A citation used to be usable exactly until the file next changed, including
when the engine itself was what changed it. `growth.proficiency` r9
(attempt-ddc1377c23fd4923a8837149538c9cd1, 200/200 turns, no finish_step) spent
54 reads and 37 writes on ONE file, ratio 1.46, and 13 of its 20 rejected
patches read `to column N is past the end of line M`: the caller was quoting
the columns it had read, resolved against a file its own earlier write had
already shifted. The remedy the error suggested — reread the range — is the
cost this module exists to remove, so suggesting it after every successful
write bought the whole mechanism back at full price.

So the engine keeps a per-(run, file) JOURNAL of the edits it published, and a
citation's line/column numbers are resolved in the text the reader was SHOWN,
then translated forward through that journal. A caller may keep using the
coordinates it read for as long as the ranges it addresses were not themselves
replaced. Two properties keep that from becoming blind editing:

* Translation is refused, not guessed, when an offset falls strictly inside a
  span an earlier edit replaced. That text is gone; nobody read the new text.
* The journal is only trusted while the file's content still matches what the
  journal says the engine left there. A write from outside the journal (the
  `write` tool, `repo_apply`, a human) breaks the chain, and citations for that
  file fall back to the original strict check — a refusal, never a silent
  best-effort apply.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections import OrderedDict

# Per-run citations kept resident. A step that cites at all cites the range it
# just read; the ledger exists to refuse the unissued, not to be a cache.
MAX_CITATIONS_PER_RUN = 1024
MAX_RUNS = 64
# Files whose edit journal a run keeps, and generations per file. A journal is
# small (three integers per reference hunk), and the loop it exists for hammers
# one file, so the per-file depth is the generous dimension.
MAX_JOURNAL_FILES = 256
MAX_GENERATIONS_PER_FILE = 4096

_SECRET = secrets.token_bytes(32)
_LOCK = threading.Lock()
_LEDGER: "OrderedDict[str, OrderedDict[str, dict]]" = OrderedDict()
# run_id -> path -> {"shas": [text sha per generation], "edits": [[(s, e, n)]]}
# shas[i] is the sha of the file's normalised text at generation i; edits[i] is
# what turned generation i into generation i+1, so len(shas) == len(edits) + 1.
_JOURNAL: "OrderedDict[str, OrderedDict[str, dict]]" = OrderedDict()


def text_sha(text: str) -> str:
    """The digest the journal identifies one version of a file's text by.

    Taken over the NORMALISED text — LF separators, no trailing newline — so
    the reader (read_tools) and the editor (strict_patch) frame a file
    identically. Two framings of the same bytes would silently break the chain
    and send every citation down the reread path this exists to retire.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(run_id: str, path: str, source: str, start_line: int,
            end_line: int, text: str) -> str:
    payload = "\x00".join((run_id, path, source, str(start_line),
                           str(end_line), text)).encode("utf-8")
    return hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()[:40]


def _bucket(store: OrderedDict, run_id: str, create: bool):
    bucket = store.get(run_id)
    if bucket is None and create:
        bucket = store[run_id] = OrderedDict()
        while len(store) > MAX_RUNS:
            store.popitem(last=False)
    if bucket is not None:
        store.move_to_end(run_id)
    return bucket


def _note_version_locked(run_id: str, path: str, file_sha: str) -> int:
    """The generation number ``file_sha`` is, seeding/resetting as needed.

    A version the journal has never seen means someone else wrote the file, so
    the old chain describes text that is gone: it is replaced rather than
    extended, and the returned generation is 0. Citations issued against the
    discarded generations then fail `chain_intact` and take the strict path.
    """
    files = _bucket(_JOURNAL, run_id, create=True)
    entry = files.get(path)
    if entry is not None and entry["shas"] and entry["shas"][-1] == file_sha:
        files.move_to_end(path)
        return len(entry["edits"])
    files[path] = {"shas": [file_sha], "edits": []}
    files.move_to_end(path)
    while len(files) > MAX_JOURNAL_FILES:
        files.popitem(last=False)
    return 0


def issue(run_id: str, *, path: str, source: str, start_line: int,
          end_line: int, start_byte: int, end_byte: int, text: str,
          start_char: int | None = None, file_sha: str = "") -> dict:
    """Record and return the citation for one served range.

    ``start_line``/``end_line`` are 1-based and inclusive — what a reader sees
    in the margin — and the range covers whole lines, so the columns are the
    range's own ends rather than a caller-chosen sub-slice.

    ``start_char`` is the window's offset in the file's normalised text and
    ``file_sha`` that text's digest. Both are what lets a later edit be
    addressed in THIS window's coordinates after the file has moved on; a
    caller that omits them gets exactly the old contract, which is a refusal
    once the file changes.
    """
    sha = _digest(run_id, path, source, start_line, end_line, text)
    record = {
        "path": path,
        "source": source,
        "start_line": start_line,
        "end_line": end_line,
        "start_col": 0,
        "end_col": len(text.split("\n")[-1]) if text else 0,
        "start_byte": start_byte,
        "end_byte": end_byte,
        "sha": sha,
    }
    if not run_id:
        # Nothing to bind it to, so nothing may be cited against it later.
        # Reported anyway, so the shape of a read result never depends on
        # whether the host happened to supply a run id.
        return {**record, "sha": "", "citable": False}
    with _LOCK:
        generation = (_note_version_locked(run_id, path, file_sha)
                      if file_sha and start_char is not None else None)
        bucket = _bucket(_LEDGER, run_id, create=True)
        bucket[sha] = {**record, "text": text, "start_char": start_char,
                       "file_sha": file_sha, "generation": generation}
        bucket.move_to_end(sha)
        while len(bucket) > MAX_CITATIONS_PER_RUN:
            bucket.popitem(last=False)
    return {**record, "citable": True}


def issue_span(run_id: str, *, path: str, source: str, from_line: int,
               from_col, to_line: int, to_col, text: str, start_char: int,
               file_sha: str) -> dict:
    """Record and return the citation for one exact sub-range of a file.

    A window citation covers whole lines; a column names a cut inside one,
    and a counted column has no redundancy: in a 28-character line 27 and 28
    are both legal, and only the caller knows which it meant. A span citation
    is the check that restores it. The engine issues one for the exact
    coordinates a caller named, over the exact text they cover, and shows the
    caller that text; an edit naming those columns must quote it back, and is
    refused unless those coordinates still cover that text.

    ``from_line``/``from_col``/``to_line``/``to_col`` are recorded exactly as
    the caller named them (a column may be None), because the citation is
    bound to that request. ``start_char`` and ``file_sha`` place the text in
    the file's normalised text, as for a window, so a later edit elsewhere in
    the file does not invalidate the span.
    """
    if not run_id:
        return {"kind": "span", "path": path, "sha": "", "citable": False}
    sha = _digest(run_id, path, f"{source}\x00span\x00{from_col}\x00{to_col}",
                  from_line, to_line, text)
    record = {
        "kind": "span",
        "path": path,
        "source": source,
        "start_line": from_line,
        "end_line": to_line,
        "from_col": from_col,
        "to_col": to_col,
        "sha": sha,
    }
    with _LOCK:
        generation = _note_version_locked(run_id, path, file_sha)
        bucket = _bucket(_LEDGER, run_id, create=True)
        bucket[sha] = {**record, "text": text, "start_char": start_char,
                       "file_sha": file_sha, "generation": generation}
        bucket.move_to_end(sha)
        while len(bucket) > MAX_CITATIONS_PER_RUN:
            bucket.popitem(last=False)
    return {**record, "citable": True}


def lookup(run_id: str, sha: str) -> dict | None:
    """The record this run issued for ``sha``; None when it never issued one."""
    if not run_id or not isinstance(sha, str) or not sha:
        return None
    with _LOCK:
        bucket = _LEDGER.get(run_id)
        record = bucket.get(sha) if bucket else None
        return dict(record) if record else None


def matches(record: dict, run_id: str, text: str) -> bool:
    """True when ``text`` is still what the citation was issued over."""
    return hmac.compare_digest(
        record["sha"],
        _digest(run_id, record["path"], record["source"],
                record["start_line"], record["end_line"], text),
    )


def journal_edit(run_id: str, path: str, *, edits, sha_before: str,
                 sha_after: str) -> bool:
    """Record what the engine just published for ``path``.

    ``edits`` are disjoint ``(start, end, new_length)`` spans in the character
    offsets of the text ``sha_before`` identifies. Returns True when the
    generation was appended; False when the file was not at ``sha_before``, in
    which case the chain is discarded rather than extended — an unexplained
    version is not something to translate coordinates through.
    """
    if not run_id or not path or not sha_before or not sha_after:
        return False
    spans = sorted((int(s), int(e), int(n)) for s, e, n in edits)
    with _LOCK:
        files = _bucket(_JOURNAL, run_id, create=True)
        entry = files.get(path)
        if entry is None or not entry["shas"] or entry["shas"][-1] != sha_before:
            files[path] = {"shas": [sha_after], "edits": []}
            files.move_to_end(path)
            while len(files) > MAX_JOURNAL_FILES:
                files.popitem(last=False)
            return False
        entry["edits"].append(spans)
        entry["shas"].append(sha_after)
        while len(entry["edits"]) > MAX_GENERATIONS_PER_FILE:
            # Dropping the oldest generation would silently re-number every
            # citation still pointing at it, so the whole chain goes and those
            # citations take the strict path instead.
            files[path] = {"shas": [sha_after], "edits": []}
            break
        files.move_to_end(path)
        return True


def break_journal(run_id: str, path: str) -> None:
    """Forget what the engine knows about ``path``'s edit history.

    Used for a mutation the journal cannot describe as spans (a delete, an
    add). The next read seeds a fresh chain; citations from before it fail
    `chain_intact` and are checked strictly.
    """
    if not run_id:
        return
    with _LOCK:
        files = _JOURNAL.get(run_id)
        if files:
            files.pop(path, None)


def chain_intact(run_id: str, path: str, generation, file_sha: str,
                 current_sha: str) -> bool:
    """True when coordinates from ``generation`` can be translated to now.

    Requires the journal to still hold the generation the citation was issued
    against AND the file to still be exactly what the journal says the engine
    last left there. The second half is what keeps an outside write from being
    papered over: it breaks the chain, and the citation is then judged by the
    original strict check.
    """
    if not run_id or generation is None or not file_sha or not current_sha:
        return False
    with _LOCK:
        entry = (_JOURNAL.get(run_id) or {}).get(path)
        if not entry:
            return False
        shas = entry["shas"]
        return (0 <= generation < len(shas) and shas[generation] == file_sha
                and shas[-1] == current_sha)


def remap(run_id: str, path: str, generation: int, offset: int):
    """``offset`` in ``generation``'s coordinates, in the current text's.

    None when the offset falls strictly inside a span an edit replaced: that
    text no longer exists and nobody has read what took its place, so there is
    nothing honest to return. An offset exactly at a span's start stays put
    (an insertion there lands before the earlier edit) and one at or past its
    end moves by the accumulated length change.
    """
    with _LOCK:
        entry = (_JOURNAL.get(run_id) or {}).get(path)
        if not entry or not (0 <= generation < len(entry["shas"])):
            return None
        generations = [list(g) for g in entry["edits"][generation:]]
    position = offset
    for spans in generations:
        delta = 0
        for start, end, new_length in spans:
            if position <= start:
                break
            if position < end:
                return None
            delta += new_length - (end - start)
        position += delta
    return position


def journal_depth(run_id: str, path: str) -> int:
    """Generations recorded for ``path`` — 0 when nothing is known."""
    with _LOCK:
        entry = (_JOURNAL.get(run_id) or {}).get(path)
        return len(entry["edits"]) if entry else 0


def forget_run(run_id: str) -> None:
    with _LOCK:
        _LEDGER.pop(run_id, None)
        _JOURNAL.pop(run_id, None)
