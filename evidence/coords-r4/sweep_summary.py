"""Summarise the sweep logs: outcome counts per run, and every refusal by the
table row that produced it (the per-message 'untranslated' rows collapsed).

Usage: python3 sweep_summary.py <log>...
"""
import ast
import sys
from collections import Counter

for path in sys.argv[1:]:
    counts, rows = {}, Counter()
    for line in open(path):
        if line.startswith("COUNT "):
            _, key, value = line.split()
            counts[key] = int(value)
        elif line.startswith("ROW "):
            key, value = line[4:].rsplit(" ", 1)
            outcome, kind, row = ast.literal_eval(key)
            if row.startswith("untranslated"):
                row = "untranslated (journal broken)"
            rows[(outcome, row)] += int(value)
    print("==", path.rsplit("/", 1)[-1])
    print("  ", " ".join("%s=%d" % kv for kv in sorted(counts.items())))
    print("   WRONG_WRITE=%d WROTE_EXPECTED_REFUSAL=%d" % (
        counts.get("WRONG_WRITE", 0), counts.get("WROTE_EXPECTED_REFUSAL", 0)))
    for (outcome, row), n in sorted(rows.items()):
        print("   %-14s %-55s %d" % (outcome, row, n))
    for outcome in ("FALSE_REFUSAL", "REFUSED_OK"):
        attributed = sum(n for (o, _), n in rows.items() if o == outcome)
        if rows:
            print("   %s attributed %d of %d" % (outcome, attributed, counts.get(outcome, 0)))
