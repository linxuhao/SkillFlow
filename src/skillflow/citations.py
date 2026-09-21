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

_SECRET = secrets.token_bytes(32)
_LOCK = threading.Lock()
_LEDGER: "OrderedDict[str, OrderedDict[str, dict]]" = OrderedDict()


def _digest(run_id: str, path: str, source: str, start_line: int,
            end_line: int, text: str) -> str:
    payload = "\x00".join((run_id, path, source, str(start_line),
                           str(end_line), text)).encode("utf-8")
    return hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()[:40]


def issue(run_id: str, *, path: str, source: str, start_line: int,
          end_line: int, start_byte: int, end_byte: int, text: str) -> dict:
    """Record and return the citation for one served range.

    ``start_line``/``end_line`` are 1-based and inclusive — what a reader sees
    in the margin — and the range covers whole lines, so the columns are the
    range's own ends rather than a caller-chosen sub-slice.
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
        bucket = _LEDGER.get(run_id)
        if bucket is None:
            bucket = _LEDGER[run_id] = OrderedDict()
            while len(_LEDGER) > MAX_RUNS:
                _LEDGER.popitem(last=False)
        _LEDGER.move_to_end(run_id)
        bucket[sha] = {**record, "text": text}
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


def forget_run(run_id: str) -> None:
    with _LOCK:
        _LEDGER.pop(run_id, None)
