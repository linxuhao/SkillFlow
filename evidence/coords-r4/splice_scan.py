"""Splice-damage and phrase scan over the given files.

Reports: identical adjacent non-blank lines, a line that ends with a comma or
opener immediately followed by a line repeating its tail (a stale tail), a
line longer than 100 characters inside a docstring/comment block that looks
fused (two sentences joined without a space), and the forbidden phrases.
Usage: python3 splice_scan.py <file>...
"""
import re
import sys

PHRASES = ("not a defect", "by design", "documented", "known limitation",
           "intentionally")
total = 0
for path in sys.argv[1:]:
    lines = open(path, encoding="utf-8").read().split("\n")
    hits = []
    for i in range(1, len(lines)):
        a, b = lines[i - 1].strip(), lines[i].strip()
        if a and a == b and a not in ("}", ")", "]", '"""', "else:", "try:", "---", "```", "|"):
            hits.append((i + 1, "DUPLICATE_ADJACENT", b[:100]))
    for i, line in enumerate(lines, 1):
        if re.search(r"[a-z][.;:][A-Z][a-z]", line) and "http" not in line and "::" not in line:
            hits.append((i, "FUSED?", line.strip()[:120]))
        if re.search(r'(\S{3,})",\1', line) or re.search(r'",[a-z]{1,3}",', line):
            hits.append((i, "STALE_TAIL?", line.strip()[:120]))
        low = line.lower()
        for p in PHRASES:
            if p in low:
                hits.append((i, "PHRASE " + p, line.strip()[:120]))
    print("==", path, "lines", len(lines), "hits", len(hits))
    for h in hits:
        print("  ", *h)
    total += len(hits)
print("TOTAL_HITS", total)
