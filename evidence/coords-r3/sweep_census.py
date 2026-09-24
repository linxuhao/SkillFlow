"""Every probe of the sweep, with its V4A outcome against where the line went.

Prints the cross product, every probe that wrote somewhere other than where
its old line went (must be none), and every refusal of a window whose line
survived the edit (false refusals), by kind.
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, "tests")
import test_the_journal_records_what_the_applier_wrote as t

total = 0
wrong_place = []
false_refusals = []
for (shape, edit), ident in zip(t.EDITS, t.IDS):
    lines = t.SHAPES[shape]
    n = len(lines)
    after, moved = t._after(lines, edit)
    kind, k, _ = edit
    for probe in t._probes(n):
        total += 1
        with tempfile.TemporaryDirectory() as d:
            applied, error, result = t._outcome(Path(d), lines, edit, "v4a", probe)
        i = probe[1]
        want = None
        if probe[0] == "window":
            if moved[i] is None:
                want = "refused"
            else:
                e = list(after); e[moved[i]] = "Y"; want = e
        elif kind == "insert":
            e = list(after)
            at = (0 if (k == 0 and i == 0) else moved[i]) if i < n else moved[n - 1] + 1
            e.insert(at, "P"); want = e
        if want is None:
            continue
        if applied is False:
            if want != "refused":
                false_refusals.append((ident, probe, lines, error.split(": ", 2)[-1][:60]))
        elif want == "refused" or result != want:
            wrong_place.append((ident, probe, lines, result, want))
print("EDITS", len(t.EDITS), "PROBES", total, "TRANSLATIONS_BOTH_FORMATS", 2 * total)
print("WRONG_PLACE", len(wrong_place))
for w in wrong_place:
    print("  WRONG", *w)
print("FALSE_REFUSALS", len(false_refusals))
for f in false_refusals:
    print("  FALSE", *f)
