# editing.the-coordinates-must-come-from-the-read-too (rev 5): delivery notes, round 4

- Attempt: `attempt-6b1d63b7593346ab88135fb0f9848cfb`. Branch: `director/coords-r4-20260924`.
- Base: the r3 candidate `6483fdf8c98bad23ede4d4d98ef0e47654a72fad`. Release base: `101da5c44e23aadbb0dcf3a423a0ac43286453bc` (1.5.79).
- The r3 review is `~/.AItelier/director/reports/coords-r3-review-20260924/review.md` (sha256 `e103617c1104c5f62a2269c9e6b2caa9fabfaf5cff379ab4b63f39c08034e931`), called `$R3` below. The r1 and r2 review directories are `$R1` and `$R2`.
- The r3 review scripts, run unmodified:
  - `probe6.py` sha256 `115a86005a3583647f0de48ba5d722500852d353357445922d9ce452a2926730`;
  - `probe8.py` sha256 `f3d38d4a39d4e421c1a2259dddff553f671f10fec02135ed3ffebc31356dbdab`;
  - `fuzz7.py` sha256 `e321c1c9e8cd4c626553b110cba0a3f161ea98c6c0012dd154e1ef07f7672b4c`;
  - `fuzz8.py` sha256 `0f5361ec1d5312d41ca56f07a91f80c717249c4250fd6cc0e23e65f099e07a0d`.
- Written 2026-09-24. The last measurement below finished at 21:34Z (`r_accounts_candidate.txt`); `date -u` read 21:35:03Z when this section was written.
- Every log named here is in this directory (`evidence/coords-r4/`) as `.txt`, unless it names `$R1`, `$R2` or `$R3`.
- Every run used a throwaway `docker run --rm --init -m 3g aitelier:latest`, with the tree's `src/` first on `PYTHONPATH`. Each log prints `IMPORT_PROOF <tree>/src/skillflow/__init__.py` and the sha256 of `strict_patch.py` and `citations.py`.
  - Candidate: `2a64a0aa…` (`strict_patch.py`) and `284ebb51…` (`citations.py`) in every candidate log, the sweep logs included.
  - Base 101da5c: `5340112d…` and `88a0d9c8…`.
- Every RC below was captured bare (`$?`, no pipe). The production `aitelier` container was not touched.
- **The container cap was exceeded once.** See "Container count" at the end.

## What the last green light was bought with

r2 and r3 were green on the director's ruling (1): "this run's own edits are translated through the frame, not refused". Each round fixed the named shapes, and the next review found new ones beside them. Both of r3's remaining shapes come from one root, as the goal states:
- **A replaced blank line was journaled as a pure insertion.** Its removed `\n` never reached the journal. So the "shown bytes were changed" check could not see it, and C1b wrote `def g():    x = 1`.
- **A point on this run's insertion offset has no bytes of its own to follow.** Before or after the inserted text is a guess. r3's own test pinned one guess as correct (`at = 0 if (k == 0 and i == 0)`).

**The property that gave way:** "a citation writes only where it was shown". It has one reading only when both of these hold:
- every edit is journaled as `(removed [a, b), inserted text)`;
- a point that meets an insertion offset is refused.

The observation point for the second is the sweep and the three mutants below. Before this round, nothing observed the first: no test compared a journaled edit's removed range with the bytes the edit removed. Mutant (b), below, is now that observation point.

## What changed

1. **`src/skillflow/citations.py`: `translate()` is the goal's table, and nothing else places an older citation.**
   - `remap()` and `touched()` are deleted. `translate(run, path, generation, start, end)` returns `(start, end)` or one of three refusals:
     - `REMOVED`: an edit's removed range overlaps the citation;
     - `SPLIT`: a pure insertion strictly inside a non-empty citation;
     - `POINT`: an empty citation exactly at a pure insertion's offset.
   - A pure insertion at a non-empty citation's start moves it; one at its end stays outside it. Everything else shifts by the length change.
   - For an empty citation at `p`, "overlaps" is read as `a <= p < b`: the character just after the point was removed. This reading is stated in the docstring. It is the one the r3 review's `fuzz7.py` oracle uses ("pbefore i: … if line i was deleted, a refusal").
2. **`src/skillflow/strict_patch.py`: every edit is journaled as removed range plus written length, in offsets where every line ends in its own newline.**
   - `_terminated(text, data)` is the joined text plus `"\n"` (or `""` for an empty file). A blank line is one character wide.
   - A V4A block (`_block_spans`) is recorded as `[start of line k, start of line k+m)` removed and the new lines written, each with its newline. A replaced blank line therefore removes its `\n` (b > a).
   - A reference write (`_recorded`) is recorded as follows.
     - Whole lines (no columns) are recorded with their newline: `(start, end+1, len+1)`.
     - A column write that starts at one line's end and stops at another line's end, and whose text begins with that newline (or which is a deletion), is the whole-line edit `"\n" + text`. It is recorded one character later, at the next line's start, where V4A records the same change (see "Conventions" below).
     - **Two readings.** When that write starts on a blank line, and the removed and the written text each end with `\n` (or are empty), the same call means "before the blank line" or "after it". The blank line is then recorded as rewritten. For a pure insertion that is `[(p, p+1, 1), (p+1, p+1, n)]`, and older citations the two readings would place differently are refused.
   - `_journal` checks `_spans_account_for` on the terminated texts; a failure breaks the chain (every older citation is refused).
   - The window path in `cited_bytes` translates `[s, e + own)` with `own = 1` for whole-line windows, and gives three distinct messages (quoted in the tests):
     - `REMOVED`: "lines {f}-{t} changed since the digest was issued: they were removed or replaced by an earlier edit in this run; reread the range and cite the new digest";
     - `SPLIT`: "…: an earlier edit in this run inserted text inside them; reread…";
     - `POINT`: "columns {c}..{c} are an insertion point…".
   - `_span_range` refuses `POINT` with "span {coords} is an insertion point and an earlier edit in this run inserted text exactly there, so which side of that text it meant cannot be told; reread the range and resend the reference with the new digest to be shown where it lands now". `REMOVED`/`SPLIT` raise the existing stale message. The text comparison after translation is unchanged.
3. **`tests/test_the_journal_records_what_the_applier_wrote.py`** is rewritten, 322 tests:
   - the review cases, pinned verbatim with their sources (table below);
   - a new M3 test, `test_a_span_rechecks_its_text_when_the_journal_misdescribes_the_edit`;
   - the table sweep (186 cases);
   - the reference-vs-V4A journal equality (100 cases).
   - **`at = 0 if (k == 0 and i == 0)` is deleted.** The new oracle refuses a point at any insertion offset and has no special case for line 1.
4. **`tests/test_a_citation_writes_only_where_it_was_shown.py`**: only the T1 docstring changed. The case is now refused by the translation (a removed range overlaps the span). The docstring points to the new M3 test, which exercises the text comparison on its own.

### Why the M3 test is new

r1's review mutant M3 drops the span's text comparison. On r3 it was killed by `test_T1_the_shown_text_edited_after_the_preview_is_refused`, because r3's translation let that span through to the comparison. The table now refuses T1 before the comparison (`REMOVED`). So on the first full run of this round, M3 **survived**: SUITE_m3_RC=0, 0 ignitions. That run's logs were overwritten when stage 2 and the mutants were rerun after the test was added, so this number has no log in this directory.

The applier's journal always accounts for the edit (`_spans_account_for`), so the comparison can only fire when a journal entry misdescribes the edit. The new test writes such an entry directly with `citations.journal_edit` and expects the span to be refused by the comparison. M3 is now killed (table below).

## Route and discriminating reason (criterion 1)

**The route is unchanged from r1–r3: caller-supplied digest redundancy.** The caller quotes a digest the engine issued, and never a count or the original text.
- Explicit columns must cite a span the engine issued for exactly those coordinates, after it showed the covered text.
- The common intent needs no column at all: the whole window (sha only) or whole lines (lines only).

**This round's discriminating reason: a citation has one reading only if the journal can say which bytes it removed.**
- A digest resolves through the journal.
- A replacement recorded as a pure insertion hides the bytes it removed, so no check downstream can see them.
- A point has no bytes, so when an insertion lands on it, no journal can say which side it meant. Only a refusal is not a guess.
- A whole-window route would face the same two facts: a whole window cited across a replaced blank line needs the same removed-range record. So the choice of route does not change this round's work.

### Conventions this round uses that the goal's table does not spell out

Each one is stated here, in the code docstrings and in the test file, so that none of them can pass as a table row.
1. **A whole-line window owns its newline.** A blank line is then one character wide. This is what lets B2 translate, as the goal requires. Without it a blank line is a point, and B2's insertion right below it would meet the POINT row.
2. **A newline-first whole-line edit is recorded at the next line's start.** `"\n" + text` at the end of line k-1 and `text + "\n"` at the start of line k produce the same bytes, so the edit has two alignments. This round records the one V4A records.
   - Given convention 1, the other alignment removes line k-1's own newline (a deletion) or inserts strictly inside line k-1's window (an insertion). The window on line k-1 would then be refused (REMOVED or SPLIT) although line k-1 survived.
   - The sweep oracle's rule "a point at S[k] is refused" follows from this convention for the newline-first form. For the V4A and line-start forms it is the table itself.
   - The other alignment would move one false refusal from the point before line k to the point after line k-1; neither is a wrong write.
3. **Two readings on a blank line are refused.** One reference call (coordinates plus text) can be sent by two different edits: on a blank line, `"\n"` at its end appends a blank line after it, and `"\n"` at its start inserts one before it. The engine records the call as a rewrite of the blank line. The sweep oracle expects a refusal exactly where the two edits' answers disagree (`_oracle`).
4. **An empty citation overlaps a removed range when `a <= p < b`** (section "What changed", item 1).

## a-mis-specified-range-cannot-write-silently

### The review's cases, pinned verbatim

Each case is a test in `tests/test_the_journal_records_what_the_applier_wrote.py`. The test docstring cites the file each came from. The review scripts were also re-run unmodified on this tree (`run_py.sh`, driver `stage1_driver.txt`): PROBE_RC, PROBE2_RC, PROBE3_RC, PROBE4_RC, PROBE5_RC, PROBE6_RC and PROBE8_RC are all 0. Case-by-case comparison with the review's own r3 logs: `probe*_cases_vs_r3review.txt` (`case_compare_driver.txt`, each RC 0).

**Refused:**

| case | source; `$R3/review.md` lines | this tree (log) | r3 (review's log) |
|---|---|---|---|
| C1b | probe8.py; 110, 117, 224 | refused `removed(1, 2)`, file unchanged `def f():\n    x = 1\n    return 1\n` (`probe8_candidate.txt`) | wrote `def g():    x = 1` |
| C1, C1s | probe8.py; 117, 224 | refused `removed(1, 2)` | wrote |
| C1r | probe8.py; 117, 225 | refused `removed(1, 2)` | wrote |
| C2 | probe8.py; 118, 226 | refused `removed(2, 3)`, file `b\n\n\nc\n` | wrote `b\nY\nc\n` |
| C3 | probe8.py; 145, 227 | refused `point(1, 0)`, file `# header\n\nimport os\n` | wrote `\nP# header` |
| C4 | probe8.py; 148, 227 | refused `point(1, 0)`, file `N\n\n\n` | wrote `\nPN` |
| T1 | probe6.py; 127–139, 210 | refused `point(1, 0)` (`probe6_candidate.txt`) | wrote `import sys` above the docstring |
| H3 (retracted by rev 5) | `$R2/probe4.py`; review 45 | refused `point(2, 0)`, file `a\n\n\nb\n` (`probe4_candidate.txt`) | wrote `a\n\n# note\n\nb\n` |

**Intended bytes:**

| case | source; `$R3/review.md` lines | intended = this tree | r3 |
|---|---|---|---|
| T2a (V4A and reference) | probe6.py; 164, 214 | `# header\nimport sys\nx = 1\ny = 2\n` | refused |
| T2c (both) | probe6.py; 165, 214 | `# header\nimport sys\nx = 9\ny = 2\n` | refused |
| T2d (both) | probe6.py; 166, 214 | `# header\nimport sys\nx = 1\ny = 2\n` | refused |
| T2e | probe6.py; 167, 214 | `# header\nimport sys\nx = 1\n` | refused |
| T2b (both) | probe6.py; 168, 215 | `# header\nimport os\nx = 9\ny = 2\n` | same |
| B1 | probe6.py; 169, 223 | `a\nN\nY\nb\n` | same |
| B2 | probe6.py; 169, 223 | `a\nY\nN\nb\n` | refused |
| B3 | probe6.py; 169, 223 | `A\nY\nb\n` | same |
| B4 | probe6.py; 169, 223 | `a\nY\nB\n` | same |
| B5 | probe6.py; 169, 223 | `H\na\nb\nY\nc\n` | same |

**Still correct from the r2 review:**
- H1, H2, H4, H5 write `a\nx\nY\nb\n`.
- H6 and H6c are refused with `removed(2, 2)`.

**Both polarities of the new test file:**
- This tree: `new_tests_candidate.txt`, **322 passed**, NEW_CAND_RC=0 (`stage1_driver.txt`).
- The r3 tree, with the same test file (a temporary detached worktree at 6483fdf, `~/coords-r4-scratch/r3tree`, removed afterwards): `new_tests_on_r3.txt`, **206 failed, 116 passed**, NEW_R3_RC=1.
- The red tests on r3:
  - H3, T1, T1k, T1r/T1v (both), C1, C1b, C1r, C1s, C2, C3, C4;
  - T2a/c/d in both formats, T2e, B2;
  - 153 of the 186 sweep cases and 29 of the 100 equality cases.
  - H6, H6c and the two blank-line refusal tests are also refused on r3; they are red there only on the exact message text.

**Every other case in the reviews' probes**, by `case_compare.py` against the review's r3 logs:
- `probe.py` (49 cases), `probe2.py` (4), `probe3.py` (2) and `probe5.py` (5) changed nothing.
- `probe4.py` changed 8 of 37: A1, A2, A5, B3, B4, B6, C4, H3.
  - Better: A2 and B6 were wrong writes in r3 and are now refused.
  - H3 is now refused, as retracted.
  - **A1 and C4 are now refused where probe4 expected a write:** a point at the start of a line this run replaced. Row: REMOVED, convention 4.
  - **A5 is now refused where probe4 expected a write:** the same insertion point cited twice; the second meets the first's insertion. Row: POINT.
  - **B3 is now refused where probe4 expected a write:** a span over text this run replaced with identical text. Row: REMOVED.
  - **B4 now writes `DEFAULT_ZLIMIT_SECONDS`, where r3 refused.** The run inserted `Z` exactly at the start of the cited span `TIMEOUT` (3:8), then the span was cited with `LIMIT`. That is a pure insertion at a non-empty citation's end, so by the table the span follows its own bytes: `Z` stays and `TIMEOUT` becomes `LIMIT`. It is the same row as T2e. probe4 has no intended bytes for B4 ("refusal expected or acceptable"), so its `CORRECT=False` marks a write, not wrong bytes (`probe4_candidate.txt` lines 77–83).
- `probe6.py` changed 14 of 41:
  - T1 and T1r (wrong writes in r3) and T1k and T1v (correct writes in r3) are all refused now (POINT);
  - T2a, T2c, T2d, T2e and B2 now write the intended bytes;
  - T3a and T3e (writes where the review expected a refusal) are now refused;
  - R1–R3 print `accounts=False` for R1 and R2. That is because `probe6.py` (lines 238–250) calls `_spans_account_for` with the joined texts, while the spans are now in terminated offsets. `r_accounts.py` repeats the same construction with the terminated texts: `r_accounts_candidate.txt`, RACC_RC=0, `terminated_texts=True` for R1, R2 and R3.
- `probe8.py` changed 7 of 7: C1, C1b, C1r, C1s, C2, C3 and C4 all went from writing to refused.

### The sweep: WRONG-PLACE is 0

The r3 review's `fuzz7.py` and `fuzz8.py`, **unmodified**, plus `fuzz8r.py`: fuzz8 with two changes, own edits by V4A (`OWN=v4a`) or by references (`OWN=ref`). Driver: `sweep.sh`. RCs are in `sweep_driver.txt`, all 0. Summary: `sweep_summary.txt` (`sweep_summary.py`).

**Cross product, for every number in this section:**
- 400 trials per seed (seeds 7 and 8);
- a file of 2–7 lines drawn from {x, x, x, '', '', a, b}, CRLF in 15% of trials;
- 1–2 sequential patches per trial, each cut into 1–5 hunks (adjacent hunks allowed), with insertions from {x, '', N} and replacements from {x, '', N, R};
- older citations issued before the first patch: `win1` and `win2` windows, a whole-line `span`, and `pbefore`/`pend` points, per old line;
- the reviewer's oracle: where every old line went, from the hunk rows.

**The reference forms.** `OWN=ref` makes each block as a reference write:
- a replacement: a whole-line window;
- an insertion: `"\n"+text` at the end of the line above, or `text+"\n"` at the line start;
- a deletion: from the end of the line above, or from the line start.

The form is drawn per block. Seed 7 made, across 400 trials (`FORM` lines in `fuzz8r_ref_seed7.txt`):
- 316 newline-first and 301 line-start insertions;
- 93 newline-first and 81 line-start deletions;
- 447 window replacements;
- 81 two-readings calls.

Seed 8 made 287 / 280 / 85 / 75 / 411 / 67.

| run (log) | probes | CORRECT | FALSE_REFUSAL | REFUSED_OK | WRONG_WRITE | WROTE_EXPECTED_REFUSAL |
|---|---|---|---|---|---|---|
| fuzz7 seed 7 (`fuzz7_seed7.txt`) | 8055 | 3999 | 449 | 3607 | **0** | **0** |
| fuzz7 seed 8 (`fuzz7_seed8.txt`) | 7888 | 3974 | 448 | 3466 | **0** | **0** |
| fuzz8 seed 7 (`fuzz8_seed7.txt`) | 8055 | 3999 | 449 | 3607 | **0** | **0** |
| fuzz8 seed 8 (`fuzz8_seed8.txt`) | 7888 | 3974 | 448 | 3466 | **0** | **0** |
| fuzz8r V4A seed 7 (`fuzz8r_v4a_seed7.txt`) | 8055 | 3999 | 449 | 3607 | **0** | **0** |
| fuzz8r V4A seed 8 (`fuzz8r_v4a_seed8.txt`) | 7888 | 3974 | 448 | 3466 | **0** | **0** |
| fuzz8r reference seed 7 (`fuzz8r_ref_seed7.txt`) | 8048 (+7 SKIP_ALL_DELETED) | 3829 | 619 | 3600 | **0** | **0** |
| fuzz8r reference seed 8 (`fuzz8r_ref_seed8.txt`) | 7875 (+13 SKIP_ALL_DELETED) | 3828 | 594 | 3453 | **0** | **0** |

- fuzz7 and fuzz8 print a `COUNT` line only for outcomes that occurred. The WRONG_WRITE and WROTE_EXPECTED_REFUSAL zeros are the absence of those lines; `sweep_summary.txt` prints them explicitly.
- fuzz8r V4A reproduces fuzz7's and fuzz8's `COUNT` lines exactly for each seed, so its row attribution applies to them.

**Every refusal by table row** (`fuzz8r` records what `citations.translate` returned for the refused write):

| run | outcome | removed overlaps | insertion inside window | point at insertion | two-readings blank (REMOVED) | point just after a two-readings blank (POINT) | chain broken (no row) |
|---|---|---|---|---|---|---|---|
| V4A s7 | FALSE_REFUSAL 449 | 0 | 1 | 448 | – | – | 0 |
| V4A s7 | REFUSED_OK 3607 | 3297 | 261 | 49 | – | – | 0 |
| V4A s8 | FALSE_REFUSAL 448 | 0 | 0 | 448 | – | – | 0 |
| V4A s8 | REFUSED_OK 3466 | 3181 | 244 | 41 | – | – | 0 |
| ref s7 | FALSE_REFUSAL 619 | 0 | 1 | 384 | 168 | 34 | 32 |
| ref s7 | REFUSED_OK 3600 | 3178 | 231 | 49 | 127 | 4 | 11 |
| ref s8 | FALSE_REFUSAL 594 | 0 | 0 | 383 | 155 | 34 | 22 |
| ref s8 | REFUSED_OK 3453 | 3059 | 219 | 36 | 106 | 7 | 26 |

- Every refusal is attributed ("attributed 619 of 619" and so on, `sweep_summary.txt`).
- A false refusal here is one the reviewer's line oracle calls a write. It includes every POINT refusal where that oracle, which knows each line's identity, places the point unambiguously.
- **Chain broken.** 54 false refusals (32 + 22) and 37 correct refusals came from a journal that did not accept the recorded spans (`_spans_account_for` failed). The chain broke, and every older citation on that file was refused.
  - The false ones come from 4 trials, 2 per seed, listed in `fuzz8r_ref_seed{7,8}_untranslated.txt` (DUMP_SEED7_RC=0, DUMP_SEED8_RC=0).
  - In each, one batch of reference writes has two writes meeting at the same offset: a deletion ending where an insertion begins, or two insertions at one offset.
  - For example, seed 8: file `['x', '', '', 'a']`, batch `('col', (2,0), (3,0), '')` + `('col', (3,0), (3,0), '\n')`.

### Mutants: the whole suite, on disk

Each mutant runs on a throwaway copy of the tree (`tar` of the worktree without `.git`, into `~/coords-r4-scratch/mut_<m>`).
- Whole suite per mutant, bare RC. Driver: `rerun.sh`, three drivers side by side (`mutants_run_1..3.txt`; `rerun_out.txt` has DRIVER1_RC=0, DRIVER2_RC=0, DRIVER3_RC=0).
- An IGNITION is logged each time the mutant departs from what the original code would have done.
- Mutant code: `mutate_r4.py`, except M3, which uses `$R1/mutate.py`, unmodified.
- Diffs: `mutant_<m>.diff.txt`. Ignitions: `mutant_<m>_ignitions.txt`. Each `MUTATE_<m>_RC=0`.

| mutant | change | whole suite (log) | bare RC | ignitions | red tests |
|---|---|---|---|---|---|
| control | none | 1565 passed, 3 skipped (`mutant_control_suite.txt`) | SUITE_control_RC=0 | 0 | none |
| **(a) point** | the POINT refusal removed from `translate`; the point stays before the inserted text | 123 failed, 1442 passed (`mutant_point_suite.txt`) | **SUITE_point_RC=1** | **123** | H3, C3, C4, T1, T1k, T1r/T1v (both formats), and 116 sweep cases |
| **(b) blankins** | a blank line replaced by text journaled as a pure insertion again, by V4A (`_block_spans`) and by a whole-line reference (`_recorded`) | 5 failed, 1560 passed (`mutant_blankins_suite.txt`) | **SUITE_blankins_RC=1** | **84** | C1, C1b, C1r, C1s, `test_a_window_on_a_blank_line_a_reference_filled_is_refused` |
| **(c) ends** | a pure insertion at a non-empty citation's start or end refused like one inside | 163 failed, 1402 passed (`mutant_ends_suite.txt`) | **SUITE_ends_RC=1** | **163** | **T2a (V4A and reference)**, T2c (both), T2d (both), T2e, H1, H2, H5, B1, B2, and 151 sweep cases |
| collapse | r2's content-derived V4A journal (`_collapsed_edit`) put back | 51 failed, 1514 passed (`mutant_collapse_suite.txt`) | SUITE_collapse_RC=1 | 70 | H1, H2, H3, H5, H6, C1, C1b, C1s, C2, the blank-line-deleted test, and sweep/equality cases |
| M3 | `$R1/mutate.py m3`, verbatim: the span text comparison dropped | 1 failed, 1564 passed (`mutant_m3_suite.txt`) | SUITE_m3_RC=1 | 1 | `test_a_span_rechecks_its_text_when_the_journal_misdescribes_the_edit` |
| M3b | the span chain check's `raise stale` replaced by an ignition | 2 failed, 1563 passed (`mutant_m3b_suite.txt`) | SUITE_m3b_RC=1 | 3 | `test_T2_a_span_after_an_outside_write_is_refused`, `test_a_span_from_before_an_outside_write_stays_refused_after_the_run_rebuilds_the_same_text` |
| M5 (= the r2/r3 reviews' M8) | the epoch comparison in `chain_intact` removed | 1 failed, 1564 passed (`mutant_m5_suite.txt`) | SUITE_m5_RC=1 | 1 | `test_a_span_from_before_an_outside_write_stays_refused_after_the_run_rebuilds_the_same_text` |
| M6 | the framed-window refusal on a broken chain removed | 1 failed, 1564 passed (`mutant_m6_suite.txt`) | SUITE_m6_RC=1 | 5 | `test_T2_a_window_after_an_outside_write_is_refused` |
| M7 | the frame dropped from the digest (r1's digest) | 6 failed, 1559 passed (`mutant_m7_suite.txt`) | SUITE_m7_RC=1 | 7 | `test_review_case_E_…`, `test_review_case_S3_…`, `test_review_case_W_…`, `test_review_case_S3w_…`, `test_the_same_range_in_the_same_version_is_one_sha_and_in_another_is_two`, `test_T1r_T1v_…[reference]` |

- **Cross product:** 9 mutants plus an unmutated control × the whole suite (1565 tests + 3 skipped), 1 run each.
- Every mutant is killed with bare RC 1 and ignitions > 0.
- (c) turns T2a red in both formats. So a false refusal at a window end is seen, not only a wrong write.
- **r1–r3 reviews still bind.** Every test below is green in the control (1565 passed):
  - E, S3, W and S3w: red under M7;
  - r1's T2 (a span or window after an outside write): red under M3b and M6. r1's T1 is still refused (`test_T1_the_shown_text_edited_after_the_preview_is_refused`, green), now by the table's REMOVED row rather than by the text comparison;
  - H1, H2 and H5: red under (c) and collapse. H6: red under collapse;
  - M3, M3b, M7 and M8 (= M5): all killed.
- r3's `blank` mutant (the blank-line-window refusal removed) has no counterpart: `touched()` and that refusal are deleted, and (b) replaces it.

## prove-it-on-the-edit-that-actually-went-wrong

Driver: `stage2_driver.txt`. BASE_HEAD 101da5c, CAND_HEAD 6483fdf plus this round's uncommitted changes. The committed sources hash to the same sha256 values (`committed_hashes.txt`).

**eac7cacb replay.** `replay.sh` and `run_in_throwaway.sh` are copied unchanged from `evidence/coords-r3/`.

On the candidate (`replay_candidate.txt`, REPLAY_CAND_RC=0; IMPORT_PROOF is this tree):
- fixture sha256 `036440eb…`, 49447 bytes; `read(1674,1685)` reproduces trace id 17's citation.
- **A1, trace id 22 verbatim (`to_col=27`):** `applied=False`, line 1679 still `DEFAULT_TIMEOUT_SECONDS = 30`, and the file is byte-identical to the original. Error: "a column the caller counted has nothing to check it against, so columns must cite a span citation issued for exactly those coordinates. Nothing was written. reference 1 names columns 1679:0..1679:27, which cover 'DEFAULT_TIMEOUT_SECONDS = 3' and leave '' before it and '0' after it on their lines; …".
- **A2, same read, same line, no columns:** writes `DEFAULT_TIMEOUT_SECONDS = 45`, and the rest of the file is byte-identical.
- **A3, sha + new_text only:** writes `DEFAULT_TIMEOUT_SECONDS = 45`.
- **Wuxia b3983d63:**
  - B1 (trace 2354 verbatim, `to_col=58`) is refused, and the file equals the pre-edit bytes;
  - B2 and B3 write `\t"res://tests/test_martial_arts_surface_census.gd",`.

On base 101da5c (`replay_base.txt`, REPLAY_BASE_RC=0; IMPORT_PROOF is `stepflow-coords-r1-base`):
- A1 writes `DEFAULT_TIMEOUT_SECONDS = 450` silently;
- B1 writes the stale tail `…census.gd",d",`;
- A3 and B3 are refused with "missing field(s) ['from_line', 'to_line']".

**Both polarities on the director's shape** (same logs, section C). On the candidate:
- `to_col = len-1 (0..27)` → `applied=False`, line 3 unchanged, **no `= 450`**;
- `to_col = len+1 (0..29)` → `applied=False` with "to column 29 is past the end of line 3 (28 characters); reread the range", **verbatim**;
- no columns → `DEFAULT_TIMEOUT_SECONDS = 45`.
- On base: `len-1` writes `= 450`, and `len+1` gives the same error.

**73 reference tests** (`test_reference_hunks`, `test_strict_patch_parser`, `test_output_targets`):
- candidate: `reference73_candidate.txt`, **73 passed**, REF_CAND_RC=0;
- base: `reference73_base.txt`, **73 passed**, REF_BASE_RC=0.

**Byte identity of the 8 rewritten tests:**
- `rewritten_real_candidate.txt`: 8 passed, BYTES_CAND_RC=0;
- `rewritten_real_base.txt`: 8 passed, BYTES_BASE_RC=0;
- `rewritten_real_compare.txt`: **IDENTICAL 22 / 22**, keysets equal, COMPARE_RC=0. The raw hashes are `rewritten_real_{base,candidate}.json.txt`.

**Whole suites:**
- base 101da5c: `suite_base.txt`, **1209 passed, 3 skipped**, SUITE_BASE_RC=0;
- candidate: `suite_candidate.txt`, **1565 passed, 3 skipped**, SUITE_CAND_RC=0.
- r3 measured 1409 on its candidate. This round's test file has 322 tests where r3's had 166.

## replace-what-was-read-without-naming-a-column

Unchanged from r3. On the candidate (`replay_candidate.txt`, section C):
- the sha-only and lines-only writes produce `import os\n\nDEFAULT_TIMEOUT_SECONDS = 45\n\nDEBUG = False\n`, with no column computed;
- an explicit narrowed column range still works through a cited span (A1', which follows).

A1' in `replay_candidate.txt` resends the span the refusal showed. It writes `DEFAULT_TIMEOUT_SECONDS = 450`, because the caller confirmed that span (under Not done).

`replaced_lines` / `new_lines` wording (`tool.yaml`, `strict_patch.py`): unchanged from r3. The `tool.yaml` sha256 is `bedc01e2…` on the candidate in every log.

## Read-back

- `worddiff_readback.txt`: `git diff --word-diff 6483fdf` over every changed or added non-log file in this commit: the two sources, the two tests, the scripts and this note.
- `splice_scan.txt` (`splice_scan.py`) checks for adjacent duplicate lines, fused sentences, stale tails and the forbidden phrases, over every changed non-log file, this note included.
  - Its hits, each read, none a splice:
    - `FUSED?` on Python attribute calls: `citations.py:89` (`threading.Lock()`), `fuzz8r.py:77-78` (`random.Random(...)`), `r_accounts.py:13-15` (`strict_patch.Hunk(...)`);
    - `FUSED?` on this bullet's own quotation of those names, and `STALE_TAIL?` on this note's quotation of the base's defective line (`…census.gd",d",`, the wuxia field case);
    - `PHRASE` on `splice_scan.py`'s own phrase list.
  - No hit in the two sources or the two tests other than `citations.py:89`.
- I also read every changed region myself: both source diffs whole, and the rewritten test file end to end, 684 lines.

## Not done

- **False refusals the table produces** (counted above, 400 trials × 2 seeds × the cross product stated there):
  - POINT: T1k, T1v, probe4 A5, and 448/448 V4A plus 384/383 reference sweep points where the line oracle knows the side;
  - two-readings refusals at a blank line (168/155 and 34/34);
  - probe4 A1 and C4, a point at the start of a replaced range (convention 4);
  - probe4 B3, a span over an identical replacement.
- **probe4 B4 now writes `DEFAULT_ZLIMIT_SECONDS`** where r3 refused. The table's end row requires the write; probe4 expected a refusal, and prints `CORRECT=False` for it.
- **An insertion at a line's start by a column write inside a line** (not a whole-line edit) lets a window that starts there follow its bytes (the table's end row). The sweep covers only whole-line reference forms and V4A blocks, not intra-line column insertions over a window's start.
- **Adjacent writes in one batch** (a deletion ending where an insertion starts, or two insertions at one offset) break the journal. Every older citation on that file is then refused: 54 false refusals in 4 trials.
- **The M3 comparison is reachable only through a journal written outside the applier.** The applier's own journal always accounts for its edit.
- **Rows-less `Hunk`** (engine API; the only constructor in `src` is the parser) records one block per hunk. That is coarse: whole-hunk removal. Citations inside it are refused, never misplaced (probe6 R4a).
- **The frameless citation path** (a citation issued without a frame) is still judged by the original strict window check, not by the table.
- **A blind resend of a shown span writes it:** A1' writes `= 450` after the refusal showed `'DEFAULT_TIMEOUT_SECONDS = 3'`. The digest proves only that the caller cited the shown text, not that it read it.
- **Lines-only ranges are not corroborated** beyond the window's digest. A wrong `from_line`/`to_line` inside the cited window writes those lines.
- **`coding_impl.md`** and other prompt templates are out of scope.
- **`""` and `"\n"` have the same text sha** (both normalise to an empty joined text).
- **SKIP_ALL_DELETED:** 7 + 13 probes in reference trials whose script deletes every line. A whole-line window writes one line, so those trials have no reference form.
- **The container cap was exceeded once** (next section).
- No tag, version bump, PyPI build, AItelier pin change or State DAG write.

## Container count

After the resume, the first `rerun.sh` ran stage 1, which starts up to 4 of my containers at once. That was 21:26:50–21:26:57Z, while the reviewer's `wuxia-gate-atorder-88f39d5d-1868298` container was running, so **up to 5 throwaway containers ran server-wide; the cap is 4**. `docker ps` at 21:26:54Z shows `coords-r4-new-r3` beside the gate container.

At 21:27:08Z I stopped that `rerun.sh` and its `stage2.sh` by exact pid (1905761, 1906949), and their two suite containers by name (`coords-r4-suite-cand`, `coords-r4-suite-base`). I relaunched stage 2 at most 3 wide, then the three mutant drivers (`rerun.sh`, pid 1915701). Stage 1's logs from that first run are the ones cited above; they had completed (`stage1_driver.txt`). The stopped stage-2 logs were overwritten by the relaunch.

The sweep (`sweep.sh`, 15:56–15:58Z) also ran 4 of my containers at once. I did not record whether another session's throwaway container was running then, so I cannot say whether the cap held during the sweep.

## Committed hashes

`committed_hashes.txt` gives the sha256 of each changed source and test file as committed (`git show HEAD:<path>`). `citations.py` (`284ebb51…`) and `strict_patch.py` (`2a64a0aa…`) equal the hashes every candidate log printed. Both test files were last written at 21:26:34Z, before stage 1 (21:26:50Z), stage 2 and the mutants ran. The commit sha is in the final report, since a commit cannot contain its own sha.
