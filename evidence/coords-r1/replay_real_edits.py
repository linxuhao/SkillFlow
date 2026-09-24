# -*- coding: utf-8 -*-
"""Replay the two real reference edits that wrote the wrong bytes.

Run with the tree under test first on PYTHONPATH; the first line printed is the
imported module's path. Every case prints the product line and the error text.

  A. host run eac7cacb (2026-09-21), trace ids 16/17 (read) and 22/23
     (apply_patch): line 1679 `DEFAULT_TIMEOUT_SECONDS = 30`, 28 characters,
     cited with to_col=27.
  B. wuxia run b3983d63 (2026-09-24), trace ids 2340/2341 (read) and 2354/2355
     (apply_patch): tests/unit_test_runner.gd line 78, 61 characters, cited
     with to_col=58; commit 5ac8714e holds the result.
  C. the director's probe (reports/tocol-redundancy/probe.py), part C, plus
     the same intent in the column-free writing.
"""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import skillflow
from skillflow import citations
from skillflow.read_tools import unified_read
from skillflow.strict_patch import apply_code_patch

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = os.path.dirname(os.path.dirname(HERE))
print("IMPORT_PROOF", skillflow.__file__)
RUN = "replay-run"


def smap(root):
    return {"working_tree": [("repo", root)], "named": {}, "allowed": set()}


def fresh(rel, data):
    root = tempfile.mkdtemp(prefix="coords-replay-")
    os.makedirs(os.path.join(root, os.path.dirname(rel)))
    with open(os.path.join(root, rel), "wb") as fh:
        fh.write(data)
    citations.forget_run(RUN)
    return root


def line_of(root, rel, number):
    with open(os.path.join(root, rel), "rb") as fh:
        return fh.read().decode().split("\n")[number - 1]


def show(label, root, rel, number, res):
    print("  %-52s applied=%s" % (label, res.get("applied")))
    print("  %-52s line %d = %r" % ("", number, line_of(root, rel, number)))
    if res.get("error"):
        print("  %-52s error: %s" % ("", res["error"]))
    if res.get("replaced"):
        print("  %-52s replaced: %r" % ("", res["replaced"][0]["replaced"]))


# ---------------------------------------------------------------------------
print("\n=== A. host run eac7cacb ===")
A_REL = "app/settings.py"
A_DATA = open(os.path.join(TREE, "tests", "fixtures",
                           "eac7cacb_app_settings.py.txt"), "rb").read()
print("  fixture sha256", hashlib.sha256(A_DATA).hexdigest(), "bytes", len(A_DATA))
A_WANT = "DEFAULT_TIMEOUT_SECONDS = 45"


def a_read(root, start, end):
    return unified_read(smap(root), A_REL, start_line=start, end_line=end,
                        run_id=RUN)


root = fresh(A_REL, A_DATA)
served = a_read(root, 1674, 1685)                       # trace id 16
cite = served["citation"]
print("  read(1674,1685) citation", {k: cite[k] for k in (
    "start_line", "end_line", "start_col", "end_col", "start_byte", "end_byte")})
print("  (trace id 17 served start_line 1675 end_line 1684 end_col 19 "
      "start_byte 49155 end_byte 49447)")
legacy = {"file": A_REL, "from_col": 0, "from_line": 1679,          # trace id 22
          "new_text": A_WANT, "sha": cite["sha"], "to_col": 27, "to_line": 1679}
show("A1 trace id 22 verbatim (to_col=27)", root, A_REL, 1679,
     apply_code_patch("", Path(root), references=[legacy], run_id=RUN))

root = fresh(A_REL, A_DATA)
cite = a_read(root, 1674, 1685)["citation"]
res = apply_code_patch("", Path(root), references=[{
    "file": A_REL, "sha": cite["sha"], "from_line": 1679, "to_line": 1679,
    "new_text": A_WANT}], run_id=RUN)
show("A2 same read, same line, no columns", root, A_REL, 1679, res)
with open(os.path.join(root, A_REL), "rb") as fh:
    after = fh.read()
print("  A2 rest of file byte-identical:",
      after == A_DATA.replace(b"DEFAULT_TIMEOUT_SECONDS = 30\n",
                              A_WANT.encode() + b"\n"))

root = fresh(A_REL, A_DATA)
served = a_read(root, 1678, 1679)
print("  read(1678,1679) content %r" % served["content"])
res = apply_code_patch("", Path(root), references=[{
    "file": A_REL, "sha": served["citation"]["sha"], "new_text": A_WANT}],
    run_id=RUN)
show("A3 sha + new_text only", root, A_REL, 1679, res)

# ---------------------------------------------------------------------------
print("\n=== B. wuxia run b3983d63 ===")
B_REL = "tests/unit_test_runner.gd"
COMMIT = open(os.path.join(HERE, "wuxia_5ac8714e_unit_test_runner.gd.txt"),
              "rb").read()
SERVED = json.load(open(os.path.join(HERE, "wuxia_b3983d63_trace2341_served.json")))
STALE = '\t"res://tests/test_martial_arts_surface_census.gd",d",'
BEFORE_LINE = '\t"res://tests/" + "test_martial_arts_surface_census" + ".gd",'
B_WANT = '\t"res://tests/test_martial_arts_surface_census.gd",'
lines = COMMIT.decode().split("\n")
assert lines[77] == STALE, lines[77]
lines[77] = BEFORE_LINE
B_DATA = "\n".join(lines).encode()
print("  commit 5ac8714e line 78 %r" % STALE)
print("  reconstructed pre-edit line 78 %r (%d characters)"
      % (BEFORE_LINE, len(BEFORE_LINE)))

root = fresh(B_REL, B_DATA)
served = unified_read(smap(root), B_REL, start_line=70, end_line=84, run_id=RUN)
cite = served["citation"]
same = (served["content"] == SERVED["content"]
        and all(cite[k] == SERVED["citation"][k] for k in (
            "start_line", "end_line", "end_col", "start_byte", "end_byte")))
print("  read(70,84) reproduces trace id 2341 (content + range + bytes):", same)
legacy = {"file": B_REL, "from_col": 0, "from_line": 78,          # trace id 2354
          "new_text": B_WANT, "sha": cite["sha"], "to_col": 58, "to_line": 78}
res = apply_code_patch("", Path(root), references=[legacy], run_id=RUN)
show("B1 trace id 2354 verbatim (to_col=58)", root, B_REL, 78, res)
with open(os.path.join(root, B_REL), "rb") as fh:
    print("  B1 file == commit 5ac8714e bytes:", fh.read() == COMMIT)

root = fresh(B_REL, B_DATA)
cite = unified_read(smap(root), B_REL, start_line=70, end_line=84,
                    run_id=RUN)["citation"]
res = apply_code_patch("", Path(root), references=[{
    "file": B_REL, "sha": cite["sha"], "from_line": 78, "to_line": 78,
    "new_text": B_WANT}], run_id=RUN)
show("B2 same read, same line, no columns", root, B_REL, 78, res)

root = fresh(B_REL, B_DATA)
served = unified_read(smap(root), B_REL, start_line=77, end_line=78, run_id=RUN)
res = apply_code_patch("", Path(root), references=[{
    "file": B_REL, "sha": served["citation"]["sha"], "new_text": B_WANT}],
    run_id=RUN)
show("B3 sha + new_text only", root, B_REL, 78, res)

# ---------------------------------------------------------------------------
print("\n=== C. director probe shape (strict path, citation issued by hand) ===")
LINE = "DEFAULT_TIMEOUT_SECONDS = 30"
BODY = "import os\n\n%s\n\nDEBUG = False\n" % LINE
C_REL = "app/settings.py"


def probe_cite():
    return citations.issue(RUN, path=C_REL, source="code", start_line=1,
                           end_line=len(BODY.splitlines()), start_byte=0,
                           end_byte=len(BODY.encode()),
                           text="\n".join(BODY.splitlines()))


print("  len(%r) = %d" % (LINE, len(LINE)))
for label, extra in [("to_col = len   (0..28)", {"from_col": 0, "to_col": 28}),
                     ("to_col = len-1 (0..27)", {"from_col": 0, "to_col": 27}),
                     ("to_col = len+1 (0..29)", {"from_col": 0, "to_col": 29}),
                     ("no columns (lines 3..3)", {})]:
    root = fresh(C_REL, BODY.encode())
    res = apply_code_patch("", Path(root), references=[{
        "file": C_REL, "sha": probe_cite()["sha"], "from_line": 3,
        "to_line": 3, "new_text": A_WANT, **extra}], run_id=RUN)
    show(label, root, C_REL, 3, res)
root = fresh(C_REL, BODY.encode())
served = unified_read(smap(root), C_REL, start_line=2, end_line=3, run_id=RUN)
res = apply_code_patch("", Path(root), references=[{
    "file": C_REL, "sha": served["citation"]["sha"], "new_text": A_WANT}],
    run_id=RUN)
show("sha + new_text only (read line 3)", root, C_REL, 3, res)
with open(os.path.join(root, C_REL), "rb") as fh:
    print("  product bytes %r" % fh.read())
sys.exit(0)
