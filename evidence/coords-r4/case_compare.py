"""Pair the CASE blocks of a review's probe log with this tree's, by label,
and print every case whose outcome lines changed.

Usage: python3 case_compare.py <review log> <this tree's log>
"""
import re
import sys


def cases(path):
    out, label, body = {}, None, []
    for line in open(path, encoding="utf-8", errors="replace"):
        if line.startswith("CASE "):
            if label:
                out[label] = body
            label = line[5:].split(" ", 1)[0]
            body = [line.rstrip()]
        elif label and re.match(r"\s+(WROTE|VERDICT|CORRECT|actual)", line):
            body.append(line.rstrip())
    if label:
        out[label] = body
    return out


old, new = cases(sys.argv[1]), cases(sys.argv[2])
changed = 0
for label in old:
    if old[label] != new.get(label):
        changed += 1
        print("CHANGED", label)
        print("  review:", " | ".join(x.strip() for x in old[label][1:]))
        print("  here:  ", " | ".join(x.strip() for x in new.get(label, ["(missing)"])[1:]))
print("CASES", len(old), "CHANGED", changed, "MISSING_HERE", len(set(old) - set(new)))
