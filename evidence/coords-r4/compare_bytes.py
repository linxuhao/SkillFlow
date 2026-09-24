"""Compare the files the 8 rewritten tests left on base and on the candidate.

Usage: python3 compare_bytes.py <base.json> <candidate.json>
Exits 0 only when every key is on both sides with the same sha256.
"""
import json
import sys

base = json.load(open(sys.argv[1]))
cand = json.load(open(sys.argv[2]))
same = 0
for key in sorted(base):
    ok = cand.get(key) == base[key]
    same += ok
    print(key, "base==candidate", ok, base[key][:12])
print("IDENTICAL", same, "/", len(base), "(candidate keys %d)" % len(cand))
print("keysets equal", set(base) == set(cand))
sys.exit(0 if same == len(base) and set(base) == set(cand) and base else 1)
