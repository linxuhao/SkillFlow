"""probe6.py R1-R3 again, calling `_spans_account_for` with the texts the
journal records in (every line ending in its newline) instead of the joined
texts probe6.py passes. Same hunks, same `updated_bytes` spans, the same
construction as probe6.py lines 238-250."""
import skillflow
from skillflow import strict_patch
from skillflow.strict_patch import Operation, _normalised, _terminated

print("IMPORT_PROOF", skillflow.__file__)


CASES = [
    ("R1 H5 shape", "a\nx\nb\n", strict_patch.Hunk(("a", "x", "b"), ("a", "x", "x", "b"))),
    ("R2 H6 shape", "a\nx\nx\nb\n", strict_patch.Hunk(("a", "x", "x", "b"), ("a", "x", "b"))),
    ("R3 top insertion", "l1\nl2\n", strict_patch.Hunk(("l1",), ("H", "l1"))),
]
for label, text, hunk in CASES:
    before = text.encode()
    op = Operation("Update", "f.py", (hunk,))
    edits = []
    out = strict_patch.updated_bytes(before, op, edits=edits)
    old, new = _normalised(before), _normalised(out)
    joined = strict_patch._spans_account_for(old, new, edits)
    term = strict_patch._spans_account_for(_terminated(old, before), _terminated(new, out), edits)
    print("CASE", label, "out=%r spans=%r joined_texts=%s terminated_texts=%s" % (out, edits, joined, term))
