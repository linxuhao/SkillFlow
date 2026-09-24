# -*- coding: utf-8 -*-
"""coords-r4 extension of the reviewer's fuzz8 (coords-r3 review folder, run
unmodified alongside this). Two changes, nothing else in the trial generator,
the probes, the oracle or the outcome classes is altered:

OWN=v4a  the own edits are the same V4A patches fuzz8 applies. Its trials, probes
         and counts must equal fuzz8's for the same seed (compare the COUNT lines).
OWN=ref  the SAME edit scripts are applied as reference writes instead, one batch
         per patch, cited from a whole-file read taken just before that batch:
           replacement block      -> whole-line window, new_text = the new lines
           pure insertion at p    -> either '\n' + lines at the end of line p-1,
                                     or lines + '\n' at p:0 (line-start form)
           pure deletion of block -> either the end of line p-1 .. the end of the
                                     block's last line, or p:0 .. the line after
                                     the block at col 0 (line-start form)
         the form is drawn from a separate generator (FORM_SEED) so the edit
         scripts are identical to OWN=v4a. A trial whose script deletes every
         line has no reference form (a whole-line window writes one line) and
         its probes are counted as SKIP_ALL_DELETED.

Every refusal is attributed to the row of the goal's table that produced it, by
recording what `citations.translate` returned for the final write:
  removed  -> "removed range overlaps the citation"
  split    -> "pure insertion strictly inside a non-empty window"
  point    -> "a point exactly at an insertion offset"
  (s, e)   -> translated ("shift"/"follow"), then refused by the text check
  (none)   -> translate never consulted (frameless/other), with the message

fuzz8 docstring follows.

Reviewer fuzz 8 (coords-r3) = fuzz7 plus sub-classification of every non-CORRECT write.

fuzz7 docstring follows.

Reviewer fuzz 7 (coords-r3): random multi-hunk V4A edits over repeated lines,
old citations translated through them, judged against an INDEPENDENT oracle.

The oracle knows where every old line went from the hunk rows themselves (the
rows are cut from the true edit script, and each hunk is required to match
exactly once, so its match IS the true location). It does not call the engine.

Per trial: random file (heavy repeats, blanks), 1 or 2 sequential V4A patches,
each split into 1..k hunks (adjacent hunks allowed), optional CRLF / no final
newline (no-final-newline: probe6 T5b). Probes, all issued in frame 0 before the first patch:
  win1 i     window lines i..i from a whole-file read
  win2 i     window lines i..i+1
  span i     span covering all of non-empty line i
  pbefore i  zero-width span at i:0          (new_text "P\\n")
  pend i     zero-width span at end of line i (new_text "\\nP")
Expected:
  window/span: old lines survived and still adjacent -> replaced in place;
               otherwise a refusal.
  pbefore i:   P immediately before old line i (keeps_after = line i); if line
               i was deleted, a refusal (writing there is the declared gap).
  pend i:      P immediately after old line i; if deleted, a refusal.
Outcome classes: CORRECT, REFUSED (false refusal when a write was expected),
WRONG_WRITE (applied, bytes differ from expected), WROTE_EXPECTED_REFUSAL.
"""
import os
import random
import sys
import tempfile
from collections import Counter
from pathlib import Path

import skillflow
from skillflow import citations
from skillflow.read_tools import unified_read
from skillflow.strict_patch import apply_code_patch

print("IMPORT_PROOF", skillflow.__file__)
SEED = int(os.environ.get("FUZZ_SEED", "7"))
TRIALS = int(os.environ.get("FUZZ_TRIALS", "400"))
OWN = os.environ.get("OWN", "v4a")
assert OWN in ("v4a", "ref"), OWN
print("OWN", OWN)
rng = random.Random(SEED)
form_rng = random.Random(SEED * 7919 + 1)
FORMS = Counter()
_translated = []
_real_translate = citations.translate


def _recording_translate(*args, **kwargs):
    out = _real_translate(*args, **kwargs)
    _translated.append(out)
    return out


citations.translate = _recording_translate


def row_of(results, error):
    if not results:
        return "untranslated: " + (error or "")[:70]
    last = results[-1]
    if last == citations.REMOVED:
        return "removed-overlaps"
    if last == citations.SPLIT:
        return "insertion-inside-window"
    if last == citations.POINT:
        return "point-at-insertion"
    return "translated-then-text-check"


def ref_plan(lines, rows):
    """The reference batch for one edit script: [(kind, from, to, text)] with
    1-based lines; kind 'win' is a whole-line window, 'col' a column range.
    None when the script deletes every line. Also returns the 0-based lines
    of `lines` inside a column write that has two whole-line readings
    (see two_readings_at)."""
    n = len(lines)
    plan, p, i = [], 0, 0
    two_readings = set()
    while i < len(rows):
        s, t = rows[i]
        if s == " ":
            p += 1; i += 1
            continue
        j = i
        while j < len(rows) and rows[j][0] != " ":
            j += 1
        dels = [t for s, t in rows[i:j] if s == "-"]
        ins = [t for s, t in rows[i:j] if s == "+"]
        m = len(dels)
        if m and ins:
            plan.append(("win", (p + 1, None), (p + m, None), "\n".join(ins)))
            FORMS["replace-window"] += 1
        else:
            forms = []
            if p >= 1:
                forms.append("newline-first")
            if (p + m < n) if m else (p < n):
                forms.append("line-start")
            if not forms:
                return None, set()
            form = form_rng.choice(forms)
            FORMS[("delete-" if m else "insert-") + form] += 1
            if form == "newline-first":
                text = "" if m else "\n" + "\n".join(ins)
                last = p + m - 1 if m else p - 1
                plan.append(("col", (p, len(lines[p - 1])),
                             (last + 1, len(lines[last])), text))
            else:
                text = "" if m else "\n".join(ins) + "\n"
                plan.append(("col", (p + 1, 0), (p + m + 1, 0), text))
            covered = two_readings_at(lines, plan[-1])
            if covered:
                two_readings.update(covered)
                FORMS["two-readings-at-blank"] += 1
        p += m
        i = j
    return plan, two_readings


def two_readings_at(lines, write):
    """The 0-based lines covered by both whole-line readings of a column
    write that has two, else []. Stated on the bytes: the write starts on a blank line
    (its start is that line's start AND its end), stops at a line's end, and
    the text it removes and the text it writes each begin with a newline or
    are empty and each end with a newline or are empty; shifting such a write
    by one character, to the other side of the blank line, gives the same
    file. Both readings together cover [start, end + 1): the blank line it
    starts on and, when it removes text, the blank line that follows what it
    removes; a pure insertion also has an insertion offset just after the
    blank line. This is attribution only; the outcome is judged by the
    oracle."""
    _, (fl, fc), (tl, tc), text = write
    t = "".join(line + "\n" for line in lines)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    a, b = starts[fl - 1] + fc, starts[tl - 1] + tc
    removed = t[a:b]
    if not (a < len(t) and t[a] == "\n" and b < len(t) and t[b] == "\n"):
        return []
    if a > 0 and t[a - 1] != "\n":
        return []
    for part in (removed, text):
        if part and not (part.startswith("\n") and part.endswith("\n")):
            return []
    if not removed and not text:
        return []
    return [q for q in range(len(lines)) if starts[q] <= b and a < starts[q + 1]]


def apply_plan(root, run, plan):
    w = rd(root, run, 0, len(norm((root / REL).read_bytes())))["citation"]["sha"]
    refs = []
    for kind, (fl, fc), (tl, tc), text in plan:
        if kind == "win":
            refs.append({"file": REL, "sha": w, "from_line": fl, "to_line": tl,
                         "new_text": text})
            continue
        pr = ap(root, run, [{"file": REL, "sha": w, "from_line": fl, "from_col": fc,
                             "to_line": tl, "to_col": tc, "new_text": "?"}])
        if not pr.get("spans"):
            return {"applied": False, "error": "preview: %s" % pr.get("error")}
        refs.append({"file": REL, "sha": pr["spans"][0]["sha"], "new_text": text})
    return ap(root, run, refs)
REL = "f.py"
ALPHA = ["x", "x", "x", "", "", "a", "b"]
INS = ["x", "", "N"]
_n = [0]


def smap(root):
    return {"working_tree": [("repo", str(root))], "named": {}, "allowed": set()}


def rd(root, run, s, e):
    return unified_read(smap(root), REL, start_line=s, end_line=e, run_id=run)


def ap(root, run, refs=None, patch=""):
    return apply_code_patch(patch, root, references=refs, run_id=run)


def encode(lines, eol, final):
    return (eol.join(lines) + (eol if final and lines else "")).encode()


def norm(data):
    t = data.decode().replace("\r\n", "\n")
    ls = t.split("\n") if t else []
    if t.endswith("\n"):
        ls.pop()
    return ls


def edit_script(lines):
    """Rows over the whole file: (' ', l) kept, ('-', l) deleted, ('+', t) inserted."""
    while True:
        rows = []
        for i in range(len(lines) + 1):
            if rng.random() < 0.3:
                for _ in range(rng.choice([1, 1, 2])):
                    rows.append(("+", rng.choice(INS)))
            if i < len(lines):
                r = rng.random()
                if r < 0.2:
                    rows.append(("-", lines[i]))
                elif r < 0.3:
                    rows.append(("-", lines[i]))
                    rows.append(("+", rng.choice([t for t in INS + ["R"] if t != lines[i]])))
                else:
                    rows.append((" ", lines[i]))
        if any(s != " " for s, _ in rows):
            return rows


def unique(lines, old):
    framed = "\n" + "\n".join(lines) + "\n"
    needle = "\n" + "\n".join(old) + "\n"
    first = framed.find(needle)
    return first >= 0 and framed.find(needle, first + 1) < 0


def split_hunks(lines, rows):
    """Cut rows into hunks; each must change text, have old lines, match once."""
    for attempt in range(40):
        cuts = sorted(set(rng.sample(range(1, len(rows)), k=min(len(rows) - 1, rng.randint(0, 4))))) if len(rows) > 1 else []
        slices, prev = [], 0
        for c in cuts + [len(rows)]:
            slices.append(rows[prev:c])
            prev = c
        hunks, ok = [], True
        for sl in slices:
            if all(s == " " for s, _ in sl):
                continue
            old = [t for s, t in sl if s in " -"]
            new = [t for s, t in sl if s in " +"]
            if not old or old == new or not unique(lines, old):
                ok = False
                break
            hunks.append(sl)
        if ok and hunks:
            return hunks, attempt
    old = [t for s, t in rows if s in " -"]
    new = [t for s, t in rows if s in " +"]
    if not old or old == new:
        return None, -1
    return [rows], -1


def patch_text(hunks):
    body = "".join("@@\n" + "".join(s + t + "\n" for s, t in h) for h in hunks)
    return "*** Begin Patch\n*** Update File: f.py\n" + body + "*** End Patch\n"


def moved_map(rows):
    """old index -> new index (None if deleted); new line list; alt map where a
    deleted line whose block writes an IDENTICAL line at the same block offset
    counts as surviving there; whether the patch inserts at the very top."""
    moved, new, alt = [], [], []
    i = 0
    while i < len(rows):
        s, t = rows[i]
        if s == " ":
            moved.append(len(new)); alt.append(len(new)); new.append(t); i += 1
            continue
        j = i
        while j < len(rows) and rows[j][0] != " ":
            j += 1
        block = rows[i:j]
        dels = [t for s, t in block if s == "-"]
        ins = [t for s, t in block if s == "+"]
        base = len(new)
        for q, d in enumerate(dels):
            moved.append(None)
            alt.append(base + q if q < len(ins) and ins[q] == d else None)
        new.extend(ins)
        i = j
    lead = 0
    while lead < len(rows) and rows[lead][0] == "+":
        lead += 1
    # a pure insertion at the very top: '+' rows followed by a kept line (or nothing)
    top = lead > 0 and (lead == len(rows) or rows[lead][0] == " ")
    return moved, new, alt, top


def compose(m1, m2):
    return [None if a is None else m2[a] for a in m1]


def compose_top(m1, tops, top2):
    """old index -> was it the file's first line when a patch inserted at the top."""
    return [t or (a == 0 and top2) for a, t in zip(m1, tops)]


def gen_trial():
    n = rng.randint(2, 7)
    lines = [rng.choice(ALPHA) for _ in range(n)]
    eol = "\r\n" if rng.random() < 0.15 else "\n"
    final = True   # no-final-newline is covered by probe6 T5b; with a trailing
                   # blank line it is not representable as a line list
    patches, cur, moved = [], lines, list(range(n))
    alt, tops = list(range(n)), [False] * n
    twor = [False] * n
    for _ in range(rng.choice([1, 1, 2])):
        rows = edit_script(cur)
        hunks, _ = split_hunks(cur, rows)
        if hunks is None:
            return None
        m, new, a, top = moved_map(rows)
        plan, blanks = ref_plan(cur, rows) if OWN == "ref" else (None, set())
        for q, pos in enumerate(moved):
            if pos is not None and pos in blanks:
                twor[q] = True
        patches.append((patch_text(hunks) if OWN == "v4a" else plan, new, len(hunks)))
        tops = compose_top(moved, tops, top)
        moved = compose(moved, m)
        alt = compose(alt, a)
        cur = new
        if not cur:
            break
    return lines, eol, final, patches, (moved, alt, tops, twor), cur


def probes(lines):
    out = []
    n = len(lines)
    for i in range(n):
        out.append(("win1", i))
        if i + 1 < n:
            out.append(("win2", i))
        if lines[i]:
            out.append(("span", i))
        out.append(("pbefore", i))
        out.append(("pend", i))
    return out


def expected(kind, i, lines, moved, final_lines):
    if kind in ("win1", "span"):
        if moved[i] is None:
            return None
        e = list(final_lines); e[moved[i]] = "Y"; return e
    if kind == "win2":
        a, b = moved[i], moved[i + 1]
        if a is None or b is None or b != a + 1:
            return None
        e = list(final_lines); e[a:b + 1] = ["Y"]; return e
    if kind == "pbefore":
        if moved[i] is None:
            return None
        e = list(final_lines); e.insert(moved[i], "P"); return e
    if kind == "pend":
        if moved[i] is None:
            return None
        e = list(final_lines); e.insert(moved[i] + 1, "P"); return e


def run_probe(lines, eol, final, patches, kind, i):
    _n[0] += 1
    run = "fuzz7-%d" % _n[0]
    citations.forget_run(run)
    root = Path(tempfile.mkdtemp(prefix="crr3-fuzz7-"))
    (root / REL).write_bytes(encode(lines, eol, final))
    w = rd(root, run, 0, len(lines))["citation"]["sha"]
    ref = None
    if kind == "win1":
        ref = {"file": REL, "sha": w, "from_line": i + 1, "to_line": i + 1, "new_text": "Y"}
    elif kind == "win2":
        ref = {"file": REL, "sha": w, "from_line": i + 1, "to_line": i + 2, "new_text": "Y"}
    else:
        if kind == "span":
            fc, tc, text = 0, len(lines[i]), "Y"
        elif kind == "pbefore":
            fc, tc, text = 0, 0, "P\n"
        else:
            fc = tc = len(lines[i]); text = "\nP"
        pr = ap(root, run, [{"file": REL, "sha": w, "from_line": i + 1, "from_col": fc,
                             "to_line": i + 1, "to_col": tc, "new_text": "?"}])
        if not pr.get("spans"):
            return ("PREVIEW_REFUSED", (kind, i, pr.get("error")), None, None)
        ref = {"file": REL, "sha": pr["spans"][0]["sha"], "new_text": text}
    for text, new, _ in patches:
        if OWN == "ref" and text is None:
            return ("SKIP_ALL_DELETED", None, None, None)
        r = ap(root, run, patch=text) if OWN == "v4a" else apply_plan(root, run, text)
        if not r.get("applied"):
            return ("PATCH_REFUSED", r.get("error"), None, None)
        got = norm((root / REL).read_bytes())
        if got != new:
            return ("PATCH_MISAPPLIED", got, new, None)
    before = (root / REL).read_bytes()
    del _translated[:]
    res = ap(root, run, [ref])
    res = dict(res, _row=row_of(list(_translated), res.get("error")))
    after = (root / REL).read_bytes()
    citations.forget_run(run)
    return ("DONE", res, norm(before), norm(after) if after != before else None)


counts = Counter()
rows_census = Counter()
classes = Counter()
examples = {}
trials = 0
multi_hunk = 0
adjacent = 0
while trials < TRIALS:
    t = gen_trial()
    if t is None:
        continue
    lines, eol, final, patches, (moved, alt, tops, twor), final_lines = t
    trials += 1
    multi_hunk += any(h > 1 for _, _, h in patches)
    for kind, i in probes(lines):
        status, res, before, after = run_probe(lines, eol, final, patches, kind, i)
        if status != "DONE":
            counts[status] += 1
            examples.setdefault(status, (lines, [p for p, _, _ in patches], kind, i, res, before))
            continue
        exp = expected(kind, i, lines, moved, final_lines)
        if after is None:
            outcome = "REFUSED_OK" if exp is None else "FALSE_REFUSAL"
        elif exp is None:
            outcome = "WROTE_EXPECTED_REFUSAL"
        else:
            outcome = "CORRECT" if after == exp else "WRONG_WRITE"
        counts[outcome] += 1
        counts["probes"] += 1
        if after is None:
            row = res["_row"]
            cited = [i] + ([i + 1] if kind == "win2" else [])
            if row == "removed-overlaps" and any(twor[q] for q in cited):
                row += "/two-readings-at-blank"
            elif (row == "point-at-insertion" and kind in ("pbefore", "pend")
                    and i >= 1 and twor[i - 1]):
                row += "/just-after-a-two-readings-blank"
            rows_census[(outcome, kind, row)] += 1
            if outcome == "FALSE_REFUSAL" and os.environ.get("DUMP") and row.startswith(os.environ["DUMP"]):
                print("DUMP", repr((lines, [p for p, _, _ in patches], kind, i)))
        if outcome in ("WRONG_WRITE", "WROTE_EXPECTED_REFUSAL", "FALSE_REFUSAL"):
            # signature: which neighbour situation
            alt_exp = expected(kind, i, lines, alt, final_lines)
            n2 = [i] + ([i + 1] if kind == "win2" else [])
            if outcome != "FALSE_REFUSAL" and alt_exp is not None and after == alt_exp:
                sub = "identical-replacement"
            elif tops[i]:
                sub = "top-insert"
            elif kind in ("pbefore", "pend") and moved[i] is None:
                sub = "point-neighbour-deleted"
            elif any(lines[q] == "" for q in n2):
                sub = "blank-in-range"
            else:
                sub = "other"
            sig = (outcome, kind, sub)
            classes[sig] += 1
            key = sig
            if key not in examples:
                examples[key] = (lines, eol, final, [p for p, _, _ in patches], kind, i,
                                 exp, after, (res.get("error") or "")[:200])

print("SEED", SEED, "TRIALS", trials, "trials_with_multi_hunk_patch", multi_hunk)
for k in sorted(counts):
    print("COUNT", k, counts[k])
for k in sorted(classes, key=str):
    print("CLASS", k, classes[k])
for k in sorted(rows_census, key=str):
    print("ROW", k, rows_census[k])
for k in sorted(FORMS, key=str):
    print("FORM", k, FORMS[k])
for k, v in examples.items():
    print("EXAMPLE", k)
    for part in v:
        print("   ", repr(part))
print("FUZZ8R_DONE")
