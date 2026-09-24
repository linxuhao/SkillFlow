"""Apply one named, ignition-instrumented mutant to a throwaway copy of the tree.

Usage: python3 mutate_r3.py <copy> <collapse|blank|m3b|m5|m6|m7>
r2's mutate_r2.py with two mutants added for this round:
  collapse: `_collapsed_edit` put back for V4A (and every non-Cite op), which
            is what r2 journaled; ignites when its span differs from the
            applier's.
  blank:    the blank-line-window refusal removed; ignites when it would
            have refused.
M3 is applied with the review's own mutate.py, unmodified. Every replacement
here must match exactly once, or the script exits 2. An IGNITION line is
appended to <copy>/IGNITIONS.log each time the ORIGINAL code would have
refused (or, for m7, would have issued a distinct digest) and the mutant did
not.
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
    "collapse": [(SP, "def _coords(",
                  "    new = _normalised(after)\n"
                  "    if old is None or new is None or not _spans_account_for(old, new, spans):\n",
                  "    new = _normalised(after)\n"
                  "    if op.kind != \"Cite\" and old is not None and new is not None:\n"
                  "        _collapsed = _collapsed_edit(old, new)\n"
                  "        if _collapsed != [tuple(x) for x in spans]:\n"
                  "            _ignite('collapse')\n"
                  "        spans = _collapsed\n"
                  "    if old is None or new is None or not _spans_account_for(old, new, spans):\n"),
                 (SP, None, "def _journal(run_id", _COLLAPSED + "def _journal(run_id")],
    "blank": [(SP, "def _coords(",
               "                    and citations.touched(run_id, op.path, generation,\n"
               "                                          window_start + local_from)):\n",
               "                    and citations.touched(run_id, op.path, generation,\n"
               "                                          window_start + local_from)\n"
               "                    and _ignite('blank')):  # MUTANT blank: refusal removed\n")],
    # M3b: the review's mutant, with the one line the epoch argument added.
    "m3b": [(SP, "def _coords(",
             "    if not citations.chain_intact(run_id, op.path, record.get(\"generation\"),\n"
             "                                  record.get(\"file_sha\", \"\"), current_sha,\n"
             "                                  epoch=record.get(\"epoch\")):\n"
             "        raise stale\n",
             "    if not citations.chain_intact(run_id, op.path, record.get(\"generation\"),\n"
             "                                  record.get(\"file_sha\", \"\"), current_sha,\n"
             "                                  epoch=record.get(\"epoch\")):\n"
             "        _ignite('m3b')  # MUTANT m3b: chain check deleted\n")],
    # M5: the chain (epoch) comparison removed from chain_intact.
    "m5": [(CI, "def text_sha(",
            "        if epoch is not None and entry[\"epoch\"] != epoch:\n"
            "            return False\n",
            "        if epoch is not None and entry[\"epoch\"] != epoch:\n"
            "            _ignite('m5')  # MUTANT m5: epoch comparison removed\n")],
    # M6: the refusal of a framed window whose chain is broken removed
    # (back to r1: the strict line-number check decides).
    "m6": [(SP, "def _coords(",
            "            if window_start is not None:\n"
            "                # The read placed",
            "            if window_start is not None:\n"
            "                _ignite('m6')  # MUTANT m6: framed-window refusal removed\n"
            "            if False:\n"
            "                # The read placed")],
    # M7: the frame dropped from the digest (r1's digest).
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
