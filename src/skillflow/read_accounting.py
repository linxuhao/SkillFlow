"""What a run paid twice for, counted while the run is still going.

On 2026-09-21 six rounds died at their turn wall and the reason was only
knowable afterwards, by someone writing SQL against each run's own trace.db:

| round                          | reads | files | reads of bytes already served |
|--------------------------------|-------|-------|-------------------------------|
| privacy r1 (attempt-8c8461af…) |    47 |    12 |                            30 |
| gate-acct relay (…09316cc8…)   |    68 |     9 |                            50 |
| growth r9 (…ddc1377c…)         |    66 |    10 |                            54 |

A cost nobody can see during the run cannot be managed during the run, so the
count lives here, next to the surface that serves the bytes, and the same code
answers both live and offline (`skillflow.read_audit` replays a trace through
it). Two calipers, because they are two different claims and they do not agree:

`repaid_reads` — a window that overlaps lines this run was already served for
    that file. This is the one the name means. Paging a large file in disjoint
    windows is NOT repaid: it is the only way to see it.
`repeat_path_reads` — a read of any file read before, regardless of range.
    Cruder, and higher: on privacy r1 it is 35 against 30, the difference being
    five reads that opened genuinely new regions of a file already touched.
    Kept because it is the figure the 2026-09-21 measurement produced, so a
    number quoted from that day can still be reproduced instead of merely
    contradicted.

Line ranges, not bytes, are the unit: a read is charged for the lines it
served, and the spans are kept merged so N reads of one file cost N interval
insertions and not N^2 comparisons.
"""
from __future__ import annotations

import threading
from bisect import bisect_left
from collections import OrderedDict

MAX_RUNS = 64
# Files tracked per run. A run that touches more than this many files is not
# the shape this measures, and the counter must never be the thing that runs
# a host out of memory.
MAX_FILES_PER_RUN = 4096

_LOCK = threading.Lock()
# run_id -> {"reads": int, "repaid": int, "repeat_path": int,
#            "files": OrderedDict[(path, source) -> file record]}
_LEDGER: "OrderedDict[str, dict]" = OrderedDict()


def _covered(spans: list[tuple[int, int]], lo: int, hi: int) -> int:
    """Lines of the closed range [lo, hi] already inside ``spans``.

    ``spans`` is sorted, disjoint and non-adjacent.
    """
    total = 0
    index = max(0, bisect_left(spans, (lo, lo)) - 1)
    for start, end in spans[index:]:
        if start > hi:
            break
        overlap = min(hi, end) - max(lo, start) + 1
        if overlap > 0:
            total += overlap
    return total


def _merge(spans: list[tuple[int, int]], lo: int, hi: int) -> None:
    """Add [lo, hi] to ``spans`` in place, coalescing what it touches."""
    index = bisect_left(spans, (lo, lo))
    while index and spans[index - 1][1] >= lo - 1:
        index -= 1
    start, end = lo, hi
    stop = index
    while stop < len(spans) and spans[stop][0] <= hi + 1:
        start = min(start, spans[stop][0])
        end = max(end, spans[stop][1])
        stop += 1
    if index and spans[index - 1][1] >= start - 1:
        index -= 1
        start = min(start, spans[index][0])
        end = max(end, spans[index][1])
    spans[index:stop] = [(start, end)]


def record(run_id: str, *, path: str, source: str = "", start_line: int = 0,
           end_line: int = -1, spans_lines: bool = True) -> dict:
    """Charge one served window and report what of it the run already had.

    ``start_line``/``end_line`` are 1-based and inclusive, i.e. the range the
    citation reports — not the 0-based half-open arguments the caller passed,
    which say what was ASKED for rather than what came back.

    ``spans_lines=False`` charges the turn without claiming any lines were
    served — an outline. It costs a turn, so it is counted; it hands back no
    file text, so it can never be a re-read and must not be allowed to make
    the next genuine read of that region look like one.
    """
    if not run_id or not path or (spans_lines and end_line < start_line):
        return {"repaid": False, "lines": 0, "lines_already_served": 0,
                "previously_served": []}
    key = (path, source or "")
    with _LOCK:
        entry = _LEDGER.get(run_id)
        if entry is None:
            entry = _LEDGER[run_id] = {
                "reads": 0, "repaid": 0, "repeat_path": 0,
                "repaid_lines": 0, "files": OrderedDict()}
            while len(_LEDGER) > MAX_RUNS:
                _LEDGER.popitem(last=False)
        _LEDGER.move_to_end(run_id)
        files = entry["files"]
        record_for_file = files.get(key)
        first_touch_of_path = not any(p == path for p, _ in files)
        if record_for_file is None:
            record_for_file = files[key] = {
                "path": path, "source": source or "", "reads": 0,
                "repaid_reads": 0, "repaid_lines": 0, "spans": []}
            while len(files) > MAX_FILES_PER_RUN:
                files.popitem(last=False)
        spans = record_for_file["spans"]
        if spans_lines:
            already = _covered(spans, start_line, end_line)
            before = list(spans[max(0, bisect_left(
                spans, (start_line, start_line)) - 1):])
            _merge(spans, start_line, end_line)
        else:
            already, before = 0, []
            record_for_file["outline_reads"] = \
                record_for_file.get("outline_reads", 0) + 1
        entry["reads"] += 1
        record_for_file["reads"] += 1
        if not first_touch_of_path:
            entry["repeat_path"] += 1
        if already:
            entry["repaid"] += 1
            entry["repaid_lines"] += already
            record_for_file["repaid_reads"] += 1
            record_for_file["repaid_lines"] += already
        overlapping = [[s, e] for s, e in before
                       if s <= end_line and e >= start_line]
    return {
        "repaid": bool(already),
        "lines": end_line - start_line + 1,
        "lines_already_served": already,
        "previously_served": overlapping,
    }


def summary(run_id: str) -> dict:
    """The three numbers, plus which files they came from.

    ``by_file`` is ordered by repaid reads then reads, so the first entry names
    the file a round is paying for — the thing "35 of 47 were re-reads" never
    said on its own.
    """
    with _LOCK:
        entry = _LEDGER.get(run_id)
        if entry is None:
            return {"reads": 0, "distinct_files": 0, "repaid_reads": 0,
                    "repeat_path_reads": 0, "repaid_lines": 0,
                    "by_file": [], "worst_file": None}
        files = [dict(record_for_file) for record_for_file in entry["files"].values()]
        totals = (entry["reads"], entry["repaid"], entry["repeat_path"],
                  entry["repaid_lines"])
    for record_for_file in files:
        record_for_file.pop("spans", None)
    files.sort(key=lambda r: (-r["repaid_reads"], -r["reads"], r["path"]))
    reads, repaid, repeat_path, repaid_lines = totals
    return {
        "reads": reads,
        "distinct_files": len({r["path"] for r in files}),
        "repaid_reads": repaid,
        "repeat_path_reads": repeat_path,
        "repaid_lines": repaid_lines,
        "by_file": files,
        "worst_file": (files[0]["path"]
                       if files and files[0]["repaid_reads"] else None),
    }


def one_line(run_id: str) -> str:
    """The summary as a sentence a report or a log line can carry."""
    s = summary(run_id)
    if not s["reads"]:
        return "reads=0"
    worst = ""
    if s["worst_file"]:
        top = s["by_file"][0]
        worst = (f"; worst {top['path']} {top['reads']} reads "
                 f"({top['repaid_reads']} repaid)")
    return (f"reads={s['reads']} files={s['distinct_files']} "
            f"repaid={s['repaid_reads']} "
            f"(repeat-path {s['repeat_path_reads']}){worst}")


def forget_run(run_id: str) -> None:
    with _LOCK:
        _LEDGER.pop(run_id, None)


reset = forget_run
