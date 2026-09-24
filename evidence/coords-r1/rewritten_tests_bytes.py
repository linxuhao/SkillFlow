# -*- coding: utf-8 -*-
"""The eight rewritten tests' column edits, replayed call for call.

Run once with the base tree first on PYTHONPATH and once with the candidate.
Each case sends the call exactly as the ORIGINAL test sent it. On a tree that
refuses it with `spans`, the case prints the refusal and then resends citing
the spans, which is what the rewritten test does. The last line of every case
is the sha256 and repr of the file the case ends with, so the two runs can be
compared byte for byte (`compare` mode).
"""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

if sys.argv[1:2] == ["compare"]:
    base = json.load(open(sys.argv[2]))
    cand = json.load(open(sys.argv[3]))
    same = 0
    for name in base:
        equal = base[name] == cand[name]
        same += equal
        print("%-58s base==candidate bytes: %s  sha256 %s" % (name, equal, cand[name][:16]))
    print("IDENTICAL %d/%d" % (same, len(base)))
    sys.exit(0 if same == len(base) == len(cand) else 1)

import skillflow  # noqa: E402
from skillflow import citations  # noqa: E402
from skillflow.read_tools import unified_read  # noqa: E402
from skillflow.strict_patch import apply_code_patch  # noqa: E402

print("IMPORT_PROOF", skillflow.__file__)
RUN = "rewritten-bytes"
RH = "\n".join(["def answer():", "    value = 1", "    other = 2",
                "    return value", "", "def helper():", "    return 0"]) + "\n"
SW = "\n".join(["def answer():", "    first = 1", "    second = 2",
                "    third = 3", "    fourth = 4", "    return first", "",
                "def helper():", "    return 0"]) + "\n"
PRIVACY = "                 if action in _covered_actions() else []),\n"
OT = "answer = 1\nother = 2\nlast = 3\n"


def r(from_line, from_col, to_line, to_col, new_text, path="src/a.py"):
    return {"file": path, "from_line": from_line, "from_col": from_col,
            "to_line": to_line, "to_col": to_col, "new_text": new_text}


THREE = [r(2, 4, 2, 13, "value = 111"), r(4, 4, 4, 16, "return value * 2"),
         r(7, 4, 7, 12, "return 99")]
CASES = [
    ("test_reference_hunks::test_a_digest_the_read_issued_applies",
     "src/a.py", RH, (1, 2), [[r(2, 0, 2, len("    value = 1"), "    value = 41")]]),
    ("test_reference_hunks::test_reference_and_v4a_produce_the_same_bytes",
     "src/a.py", RH, (1, 4),
     [[r(2, 0, 4, len("    return value"), "    value = 41\n    return value + 1")]]),
    ("test_reference_hunks::test_one_word_cites_one_line_and_leaves_the_rest_alone",
     "src/a.py", RH, (6, 7), [[r(7, 11, 7, 12, "7")]]),
    ("test_reference_hunks::test_unordered_references_match_descending_ones_byte_for_byte",
     "src/a.py", RH, None, [[THREE[i] for i in (1, 2, 0)]]),
    ("test_output_targets::test_reference_mode_runs_end_to_end_through_the_engine",
     "original.py", OT, None,
     [[r(3, 7, 3, 8, "33", "original.py"), r(1, 9, 1, 10, "11111", "original.py")],
      [r(2, 8, 2, 9, "22", "original.py")]]),
    ("test_a_successful_write::test_citing_inside_a_span_this_run_replaced_is_refused_by_name",
     "src/a.py", SW, None, [[r(3, 4, 3, 10, "SECOND")]]),
    ("test_a_successful_write::test_the_result_says_what_it_replaced",
     "src/a.py", SW, None, [[r(2, 4, 2, 9, "    first = 1  # noted")]]),
    ("test_a_successful_write::test_the_privacy_r2_corruption_shape",
     "src/a.py", PRIVACY, None, [[r(1, 17, 1, 40, "NEW")]]),
]

out = {}
for name, rel, body, window, batches in CASES:
    print("\n==", name)
    root = Path(tempfile.mkdtemp(prefix="rewritten-"))
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(body)
    citations.forget_run(RUN)
    smap = {"working_tree": [("repo", str(root))], "named": {}, "allowed": set()}
    kw = {} if window is None else {"start_line": window[0], "end_line": window[1]}
    sha = unified_read(smap, rel, run_id=RUN, **kw)["citation"]["sha"]
    for batch in batches:
        refs = [{**ref, "sha": sha} for ref in batch]
        first = apply_code_patch("", root, references=refs, run_id=RUN)
        print("  original call: applied=%s" % first["applied"])
        if first.get("error"):
            print("  error: %s" % first["error"])
        if not first["applied"] and first.get("spans"):
            spans = {s["reference"]: s for s in first["spans"]}
            for s in first["spans"]:
                print("  shown: reference %d text=%r keeps_after=%r"
                      % (s["reference"], s["text"], s["keeps_after"]))
            refs = [{**ref, "sha": spans[i]["sha"]} if i in spans else ref
                    for i, ref in enumerate(refs, 1)]
            again = apply_code_patch("", root, references=refs, run_id=RUN)
            print("  resent citing spans: applied=%s replaced=%r" % (
                again["applied"], [e["replaced"] for e in again.get("replaced", [])]))
    data = (root / rel).read_bytes()
    out[name] = hashlib.sha256(data).hexdigest() + " " + repr(data)
    print("  final bytes sha256 %s %r" % (hashlib.sha256(data).hexdigest(), data))
json.dump(out, open(sys.argv[1], "w"), indent=1)
