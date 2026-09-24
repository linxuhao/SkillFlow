"""Apply one named, ignition-instrumented mutant to a throwaway copy of the tree.

Usage: python3 mutate_r4.py <copy> <point|blankins|ends|collapse|m3b|m5|m6|m7>
The three mutants the rev-5 goal names:
  point:    (a) the refusal of a point exactly on an insertion offset removed
            from `citations.translate`; the point then stays before the
            inserted text. Ignites each time the refusal would have fired.
  blankins: (b) a blank line replaced by text journaled as a pure insertion
            again, by V4A (`_block_spans`) and by a whole-line reference write
            (`_recorded`): its newline is left out of the removed range.
            Ignites each time such a span is recorded.
  ends:     (c) a pure insertion at a non-empty citation's start or end
            refused as if it were inside. Ignites each time it refuses one
            the table translates.
r3's mutants, with their anchors moved to r4's code:
  collapse: r2's content-derived V4A journal put back (`_collapsed_edit`, over
            the texts the journal now records in); ignites when its spans
            differ from the applier's.
  m3b, m5 (= the review's M8), m6, m7: as in r3's mutate_r3.py.
M3 is applied with the r1 review's own mutate.py, unmodified. Every
replacement here must match exactly once, or the script exits 2.
"""
import sys

LOG = ("def _ignite(name):\n"
       "    import os as _os\n"
       "    _p = _os.path.join(_os.path.dirname(__file__), '..', '..', 'IGNITIONS.log')\n"
       "    with open(_p, 'a') as _fh:\n"
       "        _fh.write('IGNITION ' + name + '\\n')\n\n\n")

_COLLAPSED = (
    "def _collapsed_edit(before, after):\n"
    "    # MUTANT collapse: r2's content-derived journal span, restored verbatim\n"
    "    if before == after:\n"
    "        return []\n"
    "    head = 0\n"
    "    limit = min(len(before), len(after))\n"
    "    while head < limit and before[head] == after[head]:\n"
    "        head += 1\n"
    "    tail = 0\n"
    "    while (tail < limit - head\n"
    "           and before[len(before) - 1 - tail] == after[len(after) - 1 - tail]):\n"
    "        tail += 1\n"
    "    return [(head, len(before) - tail, len(after) - tail - head)]\n\n\n")

SP = "src/skillflow/strict_patch.py"
CI = "src/skillflow/citations.py"
MUTANTS = {
    "point": [(CI, "def text_sha(",
               "                if start == end:\n"
               "                    if a == start:\n"
               "                        return POINT\n",
               "                if start == end:\n"
               "                    if a == start:\n"
               "                        _ignite('point')  # MUTANT point: refusal removed\n")],
    "blankins": [(SP, "def _coords(",
                  "    if whole_lines:\n"
                  "        return [(start, end + 1, len(new_text) + 1)]\n",
                  "    if whole_lines:\n"
                  "        if start == end and new_text:\n"
                  "            _ignite('blankins')  # MUTANT blankins: blank line replaced = insertion\n"
                  "            return [(start, start, len(new_text))]\n"
                  "        return [(start, end + 1, len(new_text) + 1)]\n"),
                 (SP, None,
                  "    return [(starts[k], starts[k + m], sum(len(line) + 1 for line in new))\n"
                  "            for k, m, new in blocks]\n",
                  "    out = []\n"
                  "    for k, m, new in blocks:\n"
                  "        if m == 1 and original[k] == \"\" and new:\n"
                  "            _ignite('blankins')  # MUTANT blankins: blank line replaced = insertion\n"
                  "            out.append((starts[k], starts[k], sum(len(line) + 1 for line in new) - 1))\n"
                  "        else:\n"
                  "            out.append((starts[k], starts[k + m], sum(len(line) + 1 for line in new)))\n"
                  "    return out\n")],
    "ends": [(CI, "def text_sha(",
              "                elif start < a < end:\n"
              "                    return SPLIT\n",
              "                elif start <= a <= end:\n"
              "                    if a in (start, end):\n"
              "                        _ignite('ends')  # MUTANT ends: insertion at an end refused\n"
              "                    return SPLIT\n")],
    "collapse": [(SP, "def _coords(",
                  "    if old is None or new is None or not _spans_account_for(\n"
                  "            _terminated(old, before.data), _terminated(new, after), spans):\n",
                  "    if op.kind != \"Cite\" and old is not None and new is not None:\n"
                  "        _collapsed = _collapsed_edit(_terminated(old, before.data),\n"
                  "                                     _terminated(new, after))\n"
                  "        if _collapsed != [tuple(x) for x in spans]:\n"
                  "            _ignite('collapse')\n"
                  "        spans = _collapsed\n"
                  "    if old is None or new is None or not _spans_account_for(\n"
                  "            _terminated(old, before.data), _terminated(new, after), spans):\n"),
                 (SP, None, "def _journal(run_id", _COLLAPSED + "def _journal(run_id")],
    "m3b": [(SP, "def _coords(",
             "    if not citations.chain_intact(run_id, op.path, record.get(\"generation\"),\n"
             "                                  record.get(\"file_sha\", \"\"), current_sha,\n"
             "                                  epoch=record.get(\"epoch\")):\n"
             "        raise stale\n",
             "    if not citations.chain_intact(run_id, op.path, record.get(\"generation\"),\n"
             "                                  record.get(\"file_sha\", \"\"), current_sha,\n"
             "                                  epoch=record.get(\"epoch\")):\n"
             "        _ignite('m3b')  # MUTANT m3b: chain check deleted\n")],
    "m5": [(CI, "def text_sha(",
            "        if epoch is not None and entry[\"epoch\"] != epoch:\n"
            "            return False\n",
            "        if epoch is not None and entry[\"epoch\"] != epoch:\n"
            "            _ignite('m5')  # MUTANT m5: epoch comparison removed\n")],
    "m6": [(SP, "def _coords(",
            "            if window_start is not None:\n"
            "                # The read placed",
            "            if window_start is not None:\n"
            "                _ignite('m6')  # MUTANT m6: framed-window refusal removed\n"
            "            if False:\n"
            "                # The read placed")],
    "m7": [(CI, "def text_sha(",
            "    payload = \"\\x00\".join((run_id, path, source, str(start_line),\n"
            "                           str(end_line), text, frame)).encode(\"utf-8\")\n",
            "    payload = \"\\x00\".join((run_id, path, source, str(start_line),\n"
            "                           str(end_line), text)).encode(\"utf-8\")  # MUTANT m7\n"),
           (CI, None,
            "        record[\"sha\"] = sha\n        bucket = _bucket(_LEDGER, run_id, create=True)\n",
            "        record[\"sha\"] = sha\n        bucket = _bucket(_LEDGER, run_id, create=True)\n"
            "        _old = bucket.get(sha)\n"
            "        if _old is not None and (_old.get('epoch'), _old.get('generation'), _old.get('file_sha'), _old.get('start_char')) != (epoch, generation, file_sha, start_char):\n"
            "            _ignite('m7')  # a re-issue moved an existing sha\n")],
}

copy, name = sys.argv[1], sys.argv[2]
logged = set()
for rel, anchor, old, new in MUTANTS[name]:
    path = copy + "/" + rel
    src = open(path).read()
    n = src.count(old)
    expected = 2 if (name == "m7" and anchor is None) else 1
    if n != expected:
        print("MATCHES", n, "for", name, rel)
        sys.exit(2)
    src = src.replace(old, new)
    if anchor and rel not in logged:
        assert src.count(anchor) == 1
        src = src.replace(anchor, LOG + anchor)
        logged.add(rel)
    open(path, "w").write(src)
print("MUTATED", name)
