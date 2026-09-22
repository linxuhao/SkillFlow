"""Replay a finished run's trace through the live read accounting.

The point is that this is not a second implementation. `read_accounting` is
what charges a read while the run is going; this module feeds it the reads a
`trace.db` remembers, so a number quoted about a past run and a number a
running round reports are the same measurement. A separate offline script would
be free to agree with the director's SQL and disagree with production, which is
the failure mode being avoided rather than a hypothetical.

    python -m skillflow.read_audit <trace.db> [--tree DIR] [--json]

What the trace can and cannot say, stated rather than papered over. Each
`tool_result` is stored as a 2,000-character preview, so a small read's own
`start_line`/`returned_lines` survive and a large one's are cut off before
them. Three ways a window is recovered, and the report counts each:

`exact`         the result still carries the fields; nothing is inferred.
`reconstructed` the call named start_line and end_line, so the window is those
                lines — the char cap only bites on a range wider than a
                window, which is reported when it happens.
`from_tree`     the call named no range, so the window is whatever the reader
                would serve for the whole file; recovered by running the real
                pager over the file in `--tree`. The file may have changed
                since, and a run of the audit says how many reads depended on
                that.
`assumed`       no range and no tree: the line cap is assumed. Counted
                separately because it is a guess, and a guess must never be
                indistinguishable from a measurement.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from skillflow import read_accounting
from skillflow.read_tools import _MAX_READ_LINES, _page_lines

_WINDOW = re.compile(
    r'"start_line": (-?\d+), "returned_lines": (\d+), "total_lines": (\d+)')
# Why a patch was refused, in the words the engine used. Grouped so a claim
# about which refusals a change removes can be checked class by class instead
# of against one lump called "coordinate errors".
_FAILURE_CLASSES = (
    ("column_past_end_of_line", re.compile(r"column \d+ is past the end of line")),
    ("line_past_end_of_file", re.compile(r"line \d+ is past the end of the file")),
    ("outside_cited_window", re.compile(r"fall outside the cited window")),
    ("window_changed_since_digest", re.compile(r"changed since the digest was issued")),
    ("stale_copied_context", re.compile(r"stale \(no exact match\)")),
    ("ambiguous_copied_context", re.compile(r"ambiguous \(multiple exact matches\)")),
    ("sha_never_issued", re.compile(r"was never issued by a read in this run")),
    ("malformed_patch", re.compile(
        r"must start with|Require exact|need a space|missing field|"
        r"unsupported field|unsupported syntax")),
)
# The classes a frozen coordinate frame removes by construction: the columns
# and lines are checked against the text the read served, so a coordinate that
# was valid when it was read stays valid however the rest of the file moves.
_FRAME_FIXES = ("column_past_end_of_line", "line_past_end_of_file")


def _paired(db: Path):
    """(seq, event, params, result) for every agent tool call, in order."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT seq, category, event, payload_json FROM skillflow_trace "
            "WHERE category IN ('tool_call', 'tool_result') ORDER BY seq"
        ).fetchall()
    finally:
        con.close()
    pending = None
    for seq, category, event, payload in rows:
        try:
            body = json.loads(payload)
        except ValueError:
            body = {}
        if category == "tool_call":
            pending = (seq, event, body.get("params") or {})
            continue
        if pending is None or pending[1] != event:
            pending = None
            continue
        yield pending[0], event, pending[2], body
        pending = None


def _window(params: dict, result: dict, tree: Path | None):
    """The 1-based inclusive window a read served, and how it was known."""
    hit = _WINDOW.search(result.get("preview") or "")
    if hit:
        start, returned = int(hit.group(1)), int(hit.group(2))
        if returned:
            return (start + 1, start + returned), "exact"
    start = params.get("start_line") or 0
    end = params.get("end_line")
    if isinstance(end, int) and end > start:
        return (start + 1, end), "reconstructed"
    if tree is not None:
        candidate = tree / str(params.get("path") or "")
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = None
            if text is not None:
                page = _page_lines(text, start, None,
                                   raw=bool(params.get("raw")))
                if page["returned_lines"]:
                    return ((page["start_line"] + 1,
                             page["start_line"] + page["returned_lines"]),
                            "from_tree")
    return (start + 1, start + _MAX_READ_LINES), "assumed"


def audit(db: Path, tree: Path | None = None, run_id: str = "audit") -> dict:
    """Charge every read in ``db`` and report what the run paid twice for."""
    read_accounting.forget_run(run_id)
    provenance = {"exact": 0, "reconstructed": 0, "from_tree": 0, "assumed": 0}
    # A file successfully written since the last read of it. This is the set
    # criterion `a-successful-write-does-not-invalidate-the-coordinates-of-the-
    # next-one` is about: a re-read in this state is one a durable coordinate
    # frame removes, and a re-read outside it is not, so they are counted apart
    # instead of being folded into one flattering total.
    written_since_read: set[str] = set()
    split = {"repaid_after_own_write": 0,
             "repaid_no_write_large_file": 0,
             "repaid_no_write_small_file": 0}
    patches = {"calls": 0, "failed": 0, "successful_writes": 0,
               "failure_classes": {}, "unclassified": []}
    seen_paths: set[str] = set()
    repeat_after_write = 0
    # What the retrieval tool the 2026-09-10 remedy added actually bought: the
    # tool call each search was followed by. "It is in the tool table and the
    # re-reads stayed" is the claim to check, and this is the check.
    after_search: dict[str, int] = {}
    previous_was_search = False
    for seq, event, params, result in _paired(db):
        if previous_was_search and event != "search":
            after_search[event] = after_search.get(event, 0) + 1
        previous_was_search = event == "search"
        if event == "read":
            path = str(params.get("path") or "")
            if not path:
                continue
            (lo, hi), how = _window(params, result, tree)
            provenance[how] += 1
            paid = read_accounting.record(
                run_id, path=path, source=str(params.get("source") or ""),
                start_line=lo, end_line=hi)
            after_write = path in written_since_read
            if after_write and path in seen_paths:
                repeat_after_write += 1
            if paid["repaid"]:
                if after_write:
                    split["repaid_after_own_write"] += 1
                elif _over_window(tree, path):
                    split["repaid_no_write_large_file"] += 1
                else:
                    split["repaid_no_write_small_file"] += 1
            written_since_read.discard(path)
            seen_paths.add(path)
        elif event == "apply_patch":
            patches["calls"] += 1
            error = result.get("error") or ""
            if error:
                patches["failed"] += 1
                name = next((n for n, rx in _FAILURE_CLASSES if rx.search(error)),
                            None)
                if name is None:
                    patches["unclassified"].append({"seq": seq, "error": error})
                else:
                    patches["failure_classes"][name] = \
                        patches["failure_classes"].get(name, 0) + 1
            for path in result.get("written") or []:
                patches["successful_writes"] += 1
                written_since_read.add(str(path))
    patches["removed_by_a_frozen_coordinate_frame"] = sum(
        patches["failure_classes"].get(name, 0) for name in _FRAME_FIXES)
    out = read_accounting.summary(run_id)
    out["window_provenance"] = provenance
    out.update(split)
    out["repeat_path_reads_after_own_write"] = repeat_after_write
    out["tool_after_a_search"] = after_search
    out["apply_patch"] = patches
    return out


def _over_window(tree: Path | None, path: str) -> bool:
    """Whether the file is bigger than one read window.

    From the tree, because the trace does not carry a size. Unknown counts as
    NOT over the window: it puts the read in the class no mechanism here
    claims to remove, which is the direction an unknown should fall.
    """
    from skillflow.read_tools import _MAX_READ_CHARS
    if tree is None:
        return False
    candidate = tree / path
    try:
        return candidate.is_file() and candidate.stat().st_size > _MAX_READ_CHARS
    except OSError:
        return False


def _report(name: str, data: dict) -> str:
    lines = [f"== {name}",
             f"reads                       {data['reads']}",
             f"distinct files              {data['distinct_files']}",
             f"repaid reads (overlap)      {data['repaid_reads']}",
             f"repeat-path reads           {data['repeat_path_reads']}",
             f"repaid lines                {data['repaid_lines']}",
             f"  repaid after this run's own write to that file   "
             f"{data['repaid_after_own_write']}",
             f"  repaid, no write between, file over the window  "
             f"{data['repaid_no_write_large_file']}",
             f"  repaid, no write between, file under the window "
             f"{data['repaid_no_write_small_file']}",
             f"  repeat-path reads after this run's own write     "
             f"{data['repeat_path_reads_after_own_write']}",
             f"window provenance           {data['window_provenance']}",
             f"tool called after a search  {data['tool_after_a_search']}",
             f"apply_patch                 {data['apply_patch']['calls']} calls, "
             f"{data['apply_patch']['failed']} failed; frozen frame removes "
             f"{data['apply_patch']['removed_by_a_frozen_coordinate_frame']}",
             f"  failure classes             {data['apply_patch']['failure_classes']}",
             "by file (reads / repaid / repaid lines):"]
    for entry in data["by_file"]:
        lines.append(f"  {entry['reads']:>4} {entry['repaid_reads']:>4} "
                     f"{entry['repaid_lines']:>6}  {entry['path']}")
    for other in data["apply_patch"]["unclassified"]:
        lines.append(f"  unclassified patch failure at seq {other['seq']}: "
                     f"{other['error'][:160]}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="skillflow.read_audit",
        description="What a finished run paid twice for.")
    parser.add_argument("trace", nargs="+", type=Path)
    parser.add_argument("--tree", type=Path, default=None,
                        help="the run's worktree, to recover ranged-less "
                             "reads' windows exactly")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    results = {}
    for index, db in enumerate(args.trace):
        if not db.is_file():
            print(f"no such trace: {db}", file=sys.stderr)
            return 2
        results[str(db)] = audit(db, args.tree, run_id=f"audit-{index}")
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print("\n\n".join(_report(name, data) for name, data in results.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
