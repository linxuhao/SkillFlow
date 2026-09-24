# editing.the-coordinates-must-come-from-the-read-too (rev 4): delivery notes, round 3

- Attempt: `attempt-1111e2e100b943cb8d792ef4dde4fd30`. Branch: `director/coords-r3-20260924`.
- Base: the r2 candidate `6879e9d9d18d69f3f6532ed7cf68395dcf767b2a`. Release base: `101da5c44e23aadbb0dcf3a423a0ac43286453bc` (1.5.79).
- The r2 review is `~/.AItelier/director/reports/coords-r2-review-20260924/review.md` (sha256 `c98f734176ffd6ffe7a7d300b84ff3072f71b08efa38ac26e83dbc8bead92039`), called `$R2` below. The r1 review directory is called `$R1`.
- Written 2026-09-24 (UTC). All measurements below had finished by 15:05Z (`date -u` when this line was written).
- Every log named here is in this directory (`evidence/coords-r3/`) as `.txt`, unless it names `$R1` or `$R2`.
- Every run used a throwaway `docker run --rm --init -m 3g aitelier:latest`, with the tree's `src/` first on `PYTHONPATH`. Each log prints `IMPORT_PROOF <tree>/src/skillflow/__init__.py`.
- Every RC below was captured bare (`$?`, no pipe). At most 4 of my containers ran at once. The production `aitelier` container was not touched.
- The `*_RC=` names come from the driver logs, which print each container's `$?`: `stage1_driver.txt` (new tests, probes, census), `stage2_driver.txt` (suites, reference tests, replay, bytes) and `mutants_driver.txt`.

## What the last green light was bought with

r2's translation was right, but the journal it translated through was not the edit that was made.
- A V4A edit was journaled by `_collapsed_edit`. It stripped the common prefix and suffix of the two texts and recorded the rest as one span.
- Next to a run of identical lines, that span sits at the far end of the run, not where the hunk wrote.
- The property that gave way: **what the journal records is where the edit actually wrote**. Nothing observed it. Reference edits were journaled exactly, so every test was green, and no test compared a V4A journal entry with where V4A had applied.

This round adds that observation point (the sweep below) and rederives the V4A journal from the applier.

## What changed

1. **`src/skillflow/strict_patch.py`: the journal comes from the applier, for every format.**
   - `Hunk` keeps its own rows in order (`rows: ((prefix, text), ...)`). `parse_patch` fills them.
   - `updated_bytes(before, op, edits=)`, the V4A applier, records each hunk with the line it matched at.
     - `_line_blocks` reads the changed blocks off those rows, as `(first old line, lines deleted, lines inserted)`:
       - a context row is a line kept;
       - a `-` row is the matched line it removed;
       - a `+` row is a line written before the next kept one.
     - `_block_spans` turns each block into `(start, end, new_length)` in the journal's offsets. These are the same spans a reference making the same edit records:
       - an insertion between lines k-1 and k is `"\n" + lines` at the end of line k-1;
       - an insertion at the top of the file is `lines + "\n"` at offset 0;
       - a deletion takes each line with the newline after it, or with the newline before it at the end of the file;
       - a replacement is the replaced lines' characters.
   - `_collapsed_edit` is deleted. `_journal` is the one recorder for V4A, reference, span and window edits, and records the spans the applier handed it.
   - `_spans_account_for(old, new, spans)` guards that recorder. Every character outside the spans must reappear, in order, where the spans say it moved; if not, the chain is broken (a reread), never guessed. It cannot choose between two readings of an edit; the applier supplies that choice.
   - No other content-derived diff (difflib or similar) is used anywhere.
2. **`src/skillflow/citations.py`: `touched()`.** It is used for the blank-line case the sweep found (next item).
   - A window on a blank line is a zero-width range, and its empty text matches wherever it lands. So the text check cannot catch a move.
   - `touched(run, path, generation, offset)` reports whether any journaled edit since the window's generation covered that offset or began or ended exactly at it.
   - In `cited_bytes`, a whole-line window over one blank line is refused when `touched` is true:

         line N is blank and an earlier edit in this run covered, began or ended exactly where it was, so an empty line now matches it wherever it went; reread that range and cite the new digest

   - Before this, on the r2 tree and on this tree without the check:
     - a window on a blank line this run deleted wrote `Y` into the neighbouring line;
     - after a top insertion, it wrote into the inserted line (`YN`).
   - The same happened through reference edits (`test_a_window_on_a_blank_line_a_reference_filled_is_refused` is red on r2). So it is not a V4A difference. The sweep's second test found it (census below).
3. **Criterion 3 wording** (`strict_patch.py` comment at the echo, and `src/skillflow/tools/apply_patch/tool.yaml`). `replaced_lines` and `new_lines` count line PIECES: 1 + the newlines in a non-empty text, 0 for an empty one. They do not count the net change. A whole-line insertion `x\n` at a line start reports 0 -> 2, and one line deleted with its newline reports 2 -> 0. The semantics are unchanged.
4. **`tests/test_the_journal_records_what_the_applier_wrote.py`** (new, 166 tests): the review's H cases, verbatim; three blank-line cases; and the sweep.
5. **Evidence scripts** in this directory:
   - `run_in_throwaway.sh` and `replay.sh`, copied from `coords-r2`;
   - `run_bytes.sh`, r2's with the scratch path changed;
   - `run_py.sh`, which mounts both review directories read-only;
   - `mutants.sh` and `mutate_r3.py`, which is r2's `mutate_r2.py` plus `collapse` and `blank`;
   - `sweep_census.py`.

## Route and discriminating reason (criterion 1)

**The route is unchanged from r1 and r2: caller-supplied digest redundancy.** The caller quotes a digest the engine issued, and never a count or the original text.
- Explicit columns must cite a span the engine issued for exactly those coordinates, after showing the covered text.
- The common intent needs no column at all: the whole window (sha only) or whole lines (lines only).

**This round's discriminating reason: which reading of the edit the journal records.**
- A digest resolves through the journal. So the journal must record the edit that was made, and only the code that made it knows which one that was.
- Next to repeated lines, the before and after texts have two or more valid readings. H5's `a x b -> a x x b` is "x inserted above line 2" or "x inserted below it".
- Any rule that picks one reading from content alone (prefix/suffix, difflib, a longest-match) picks the wrong one for some hunk.
- The applier does not choose. Its hunk matched at a line, and its own `-`/`+` rows say which lines it removed and where it wrote.

## a-mis-specified-range-cannot-write-silently

### The review's cases, pinned verbatim

Each case is a test in `tests/test_the_journal_records_what_the_applier_wrote.py`, citing the file it came from. The review's probe scripts were also re-run unmodified on this tree (`run_py.sh`):
- `$R2/probe4.py` → `probe4_candidate.txt`, DOCKER_RC=0;
- `$R2/probe5.py` → `probe5_candidate.txt`, DOCKER_RC=0.

| case (source) | file, citation, own edit | intended bytes | this tree | r2 (review's `$R2/probe*_candidate.txt`) |
|---|---|---|---|---|
| H1 (`probe4.py`) | `a\nx\nb\n`; span 2:0..2:1 from `rd(0,3)`; V4A `" a\n+x\n x\n b\n"` | `a\nx\nY\nb\n` | `b'a\nx\nY\nb\n'`, CORRECT=True | `b'a\nY\nx\nb\n'` |
| H2 (`probe4.py`) | same, sha-only window `rd(1,2)` | `a\nx\nY\nb\n` | `b'a\nx\nY\nb\n'`, CORRECT=True | `b'a\nY\nx\nb\n'` |
| H3 (`probe4.py`) | `a\n\nb\n`; insertion 2:0..2:0; V4A `" a\n+\n \n b\n"`; `new_text "# note\n"` | `a\n\n# note\n\nb\n` | `b'a\n\n# note\n\nb\n'`, CORRECT=True | `b'a\n# note\n\n\nb\n'` |
| H4 control (`probe4.py`) | same insertion as H1 by a reference `{sha of rd(0,1), "a\nx"}` | `a\nx\nY\nb\n` | `b'a\nx\nY\nb\n'`, CORRECT=True | same (already right) |
| H5 (`probe5.py`) | `a\nx\nb\n`; `rd(1,2)`; V4A `" a\n+x\n x\n b\n"`; `{sha, from_line 2, to_line 2, "Y"}` | `a\nx\nY\nb\n` | `b'a\nx\nY\nb\n'`, CORRECT=True | `b'a\nY\nx\nb\n'` |
| H6 (`probe5.py`) | `a\nx\nx\nb\n`; `rd(1,2)`; V4A `" a\n-x\n x\n b\n"`; lines 2..2 `"Y"` | refused | refused, file `b'a\nx\nb\n'`, CORRECT=True | wrote `b'a\nY\nb\n'` |
| H6c control (`probe5.py`) | same deletion by a reference | refused | refused, CORRECT=True | refused |

H6 and H6c are refused with the same message, and both tests assert it exactly:

    apply_patch preflight: f.py reference 1: lines 2-2 were themselves replaced by an earlier edit in this run; reread that range and cite the new digest

**H7, pasted as-is, not counted** (`probe5_candidate.txt`):

    H7 V4A applied=True -> b'def f():\n    return None\n\ndef h():\n    return None\n\ndef g():\n    return 1\n'
    CASE H7 f's shown 'return None' after V4A inserted h() ending in the same line
       WROTE=True applied=True
       intended: b'def f():\n    return None\n\ndef h():\n    return 42\n\ndef g():\n    return 1\n'
       actual:   b'def f():\n    return None\n\ndef h():\n    return 42\n\ndef g():\n    return 1\n'
       CORRECT=True

On r2 it wrote `return 42` into `f()`. The hunk's rows keep f's original `return None` as the context line at the end of the inserted `h()`, and the journal now follows those rows.

**Nothing else in the review's probes moved.** `probe4_diff_vs_review_r2.txt` and `probe5_diff_vs_review_r2.txt` diff the CASE/actual/CORRECT lines of the review's r2 run against this tree (diff RC 1 each, differences shown).
- The only changed outcomes are H1, H2, H3, H5, H6 and H7.
- Every other case prints the same outcome as on r2: A1–A5, B1–B7, C1–C4, D1–D5, E1–E5, F1–F2 and G1–G6.

**Three blank-line cases** (same test file):
- `test_a_window_on_a_blank_line_this_run_s_v4a_deleted_is_refused` is H6 with the identical lines blank (`a\n\n\nb\n`, V4A `" a\n-\n \n b\n"`). It is refused with the blank-line message.
- `test_a_window_on_a_blank_line_a_reference_filled_is_refused`: the run writes `filled` into the blank line. The window is refused.
- `test_a_window_on_a_blank_line_away_from_this_run_s_edit_still_writes`: a V4A edit two lines below leaves the blank line's window writing `a\nY\nb\nC\n`.

### The sweep: V4A and reference journal every edit identically

`test_v4a_and_reference_journal_and_translate_the_same_edit_identically` (78 parametrised cases). Each case makes one edit twice, once through V4A with whole-file context and once through a reference, each in a fresh run on a fresh file. It then asserts two things:
- the journal entry each left (`citations._JOURNAL[run][path]["edits"][-1]`) is equal;
- every older citation gets the same outcome: the same file bytes (normalised text), or refused with the same message. The citations are a whole-line window on each old line (lines i..i of a whole-file read, `"Y"`), and an insertion-point span issued before the edit at the start of each old line and at the end of the file.

**The cross product.** 6 file shapes:
- `x`;
- `x x`;
- `x x x`;
- `x '' x`;
- `'' ''`;
- `a x x b`.

Each shape gets every insertion position (0..n) × 3 inserted texts (`x`, `''`, `N`), plus every deletion position (0..n-1). That is 63 insertions + 15 deletions = **78 edits** × 2 formats.
- Each edit is probed by n windows + (n+1) insertion points = **512 probes**.
- So 78 journal comparisons and 512 outcome comparisons, over **1,024 translations** (512 per format).

`test_an_older_citation_lands_where_its_line_went_or_is_refused` (78 cases) checks the V4A outcome against where each old line went, computed from the edit itself:
- a window on a deleted line is refused;
- any other window writes `Y` at the line's new index;
- an insertion point writes `P` before its old line's new index, or at the end.

For deletion edits the 58 insertion-point probes are compared across formats only (the first test), so the oracle covers 454 of the 512 probes.

**Census** (`sweep_census_candidate.txt`, CENSUS_RC=0; script `sweep_census.py`) over those 454 probes:
- `WRONG_PLACE 0`: no probe writes anywhere other than where its line went.
- `FALSE_REFUSALS 31`: probes refused although their line survived. They are listed by name in the census and under Not done:
  - 18 are a window on old line 1 after a top insertion (6 shapes × 3 texts);
  - 13 are a blank-line window next to an edit.

**Both polarities.**
- This tree: `new_tests_candidate.txt`, **166 passed**, PYTEST_RC=0, DOCKER_RC=0.
- The r2 tree (a temporary detached worktree at 6879e9d, `~/coords-r3-scratch/r2tree`, removed afterwards), with the same test file: `new_tests_on_r2.txt`, **84 failed, 82 passed**, PYTEST_RC=1, DOCKER_RC=1. The red tests:
  - H1, H2, H3, H5 and H6;
  - the two blank-line refusal tests;
  - 36 of the 78 equality cases: every shape has at least one, e.g. `two-insert1-'x'`, `three-delete1-None` and `run_in_context-delete1-None`;
  - 41 of the 78 oracle cases.
- The 3 named tests green on r2 are H4, H6c and the blank-line-away control, all of which r2 already did right.

### Mutants: the whole suite, on disk

Each mutant runs on a throwaway copy of the tree (`tar` of the worktree without `.git`, into `~/coords-r3-scratch/mut_<m>`).
- Whole suite per mutant, bare RC. Driver: `mutants.sh`, in four parallel drivers of two mutants each (`mutants_run_1..4.txt`).
- An IGNITION is logged each time the mutant departs from what the original code would have done.
- Mutant code: `mutate_r3.py`, except M3, which uses `$R1/mutate.py`, unmodified.
- Diffs: `mutant_<m>.diff.txt`. Ignition logs: `mutant_<m>_ignitions.txt`.

| mutant | change | whole suite (log) | bare RC | ignitions | red tests |
|---|---|---|---|---|---|
| control | none | 1409 passed, 3 skipped (`mutant_control_suite.txt`) | SUITE_control_RC=0 | 0 | none |
| **collapse** | `_collapsed_edit` put back: r2's function body, and every non-`Cite` edit (V4A) journaled through it instead of the applier's spans | 78 failed, 1331 passed (`mutant_collapse_suite.txt`) | **SUITE_collapse_RC=1** | **135** | H1, H2, H3, H5, H6, `test_a_window_on_a_blank_line_this_run_s_v4a_deleted_is_refused`, 36 of the 78 equality cases and 36 of the 78 oracle cases (the full list is the `FAILED` lines of the log). The 36 equality ids are exactly the 36 that are red on the r2 tree (`new_tests_on_r2.txt`). |
| **blank** | the blank-line-window refusal removed | 8 failed, 1401 passed (`mutant_blank_suite.txt`) | **SUITE_blank_RC=1** | **58** | `test_a_window_on_a_blank_line_this_run_s_v4a_deleted_is_refused`, `test_a_window_on_a_blank_line_a_reference_filled_is_refused`, oracle `[blank_between-delete1-None]`, `[two_blanks-insert0-'x']`, `[two_blanks-insert0-'']`, `[two_blanks-insert0-'N']`, `[two_blanks-delete0-None]`, `[two_blanks-delete1-None]` |
| M3 | `$R1/mutate.py m3`, verbatim: the span text comparison dropped | 1 failed, 1408 passed (`mutant_m3_suite.txt`) | SUITE_m3_RC=1 | 1 | `test_T1_the_shown_text_edited_after_the_preview_is_refused` |
| M3b | the span chain check's `raise stale` replaced by an ignition (r2's `mutate_r2.py m3b`, unchanged) | 2 failed, 1407 passed (`mutant_m3b_suite.txt`) | SUITE_m3b_RC=1 | 3 | `test_T2_a_span_after_an_outside_write_is_refused`, `test_a_span_from_before_an_outside_write_stays_refused_after_the_run_rebuilds_the_same_text` |
| M5 (= the review's M8) | the epoch comparison in `chain_intact` removed | 1 failed, 1408 passed (`mutant_m5_suite.txt`) | SUITE_m5_RC=1 | 1 | `test_a_span_from_before_an_outside_write_stays_refused_after_the_run_rebuilds_the_same_text` |
| M6 | the framed-window refusal on a broken chain removed | 1 failed, 1408 passed (`mutant_m6_suite.txt`) | SUITE_m6_RC=1 | 5 | `test_T2_a_window_after_an_outside_write_is_refused` |
| M7 | the frame dropped from the digest (r1's digest) | 5 failed, 1404 passed (`mutant_m7_suite.txt`) | SUITE_m7_RC=1 | 6 | `test_review_case_E_…`, `test_review_case_S3_…`, `test_review_case_W_…`, `test_review_case_S3w_…`, `test_the_same_range_in_the_same_version_is_one_sha_and_in_another_is_two` |

Every mutant applied cleanly: each `MUTATE_<m>_RC=0` is in `mutants_run_1..4.txt`, and each of the four drivers returned `DRIVER<n>_RC=0` (`mutants_driver.txt`).

Coverage, as a cross product:
- 7 mutants (collapse, blank, M3, M3b, M5, M6, M7) plus an unmutated control × the whole suite (1409 tests), 1 run each.
- Every mutant is killed with bare RC 1 and ignitions > 0.
- M3, M3b, M5/M8, M6 and M7 kill the same tests as in r2. So r2's E/S3/W/S3w, T1/T2 and frame binding did not regress.

## prove-it-on-the-edit-that-actually-went-wrong

Re-measured on this tree, with the r1 script unmodified (`evidence/coords-r1/replay_real_edits.py`). Logs: `replay_candidate.txt` (SCRIPT_RC=0, DOCKER_RC=0, REPLAY_CAND_RC=0) and `replay_base.txt` (base, SCRIPT_RC=0, DOCKER_RC=0, REPLAY_BASE_RC=0).

**eac7cacb, on the real file:**
- **A1**, trace id 22 verbatim (`to_col=27`): `applied=False`, line 1679 `'DEFAULT_TIMEOUT_SECONDS = 30'`, `A1 file byte-identical to the original: True`. On base: `applied=True`, `'DEFAULT_TIMEOUT_SECONDS = 450'`.
- **A2**, same read, same line, no columns: `applied=True`, `line 1679 = 'DEFAULT_TIMEOUT_SECONDS = 45'`, `A2 rest of file byte-identical: True`.
- **A3**, sha + new_text only: `applied=True`, `line 1679 = 'DEFAULT_TIMEOUT_SECONDS = 45'`. On base: `applied=False`.

**wuxia b3983d63:** B1 (`to_col=58`) `applied=False`; B2 and B3 `applied=True`.

**The 73 reference-mode tests** (`test_reference_hunks` + `test_strict_patch_parser` + `test_output_targets`):
- candidate: `73 passed`, PYTEST_RC=0, DOCKER_RC=0, REF_CAND_RC=0 (`reference73_candidate.txt`, `IMPORT_PROOF /home/linxuhao/stepflow-coords-r1/src/skillflow/__init__.py`);
- base: `73 passed`, PYTEST_RC=0, DOCKER_RC=0, REF_BASE_RC=0 (`reference73_base.txt`, `IMPORT_PROOF /home/linxuhao/stepflow-coords-r1-base/src/skillflow/__init__.py`).

**Byte identity of the 8 tests r1 rewrote**, measured by running the real test functions:
- Method: `run_bytes.sh` (the review's runner) + `$R1/hash_tmp.py`. The base tree runs the 8 original tests and this tree runs the 8 rewritten ones, and every `.py` each left in its tmp dir is hashed.
- Runs:
  - base `8 passed`, PYTEST_RC=0, DOCKER_RC=0, BYTES_BASE_RC=0 (`rewritten_real_base.txt`);
  - candidate `8 passed`, PYTEST_RC=0, DOCKER_RC=0, BYTES_CAND_RC=0 (`rewritten_real_candidate.txt`).
- Result: `IDENTICAL 22 / 22 (candidate keys 22)`, COMPARE_RC=0 (`rewritten_real_compare.txt`; hashes in `rewritten_real_*.json.txt`). The 22 are 11 distinct files, each seen twice through pytest's `current` symlink.

No docstring or message attributes the eac7cacb error to the model.

## replace-what-was-read-without-naming-a-column

**Pole 1: no column number.** `replay_candidate.txt` part C, `sha + new_text only (read line 3)`: `applied=True`, and `no columns (lines 3..3)` gives `line 3 = 'DEFAULT_TIMEOUT_SECONDS = 45'`. On base, the sha-only form is `applied=False` (`replay_base.txt`).

**Pole 2: a narrowed sub-range still works.** The column cut `3:26..3:28 -> '45'` is refused on the first call, showing `'30'`, and written by the span on the second. This is `$R1/probe.py` P6a/P6b, unchanged since r2; its tests are in the whole suite, which is green.

**How `replaced_lines` / `new_lines` count, stated truthfully.**
- The echo comment in `cited_bytes` and the `tool.yaml` description now say they count line pieces: 1 + the newlines in a non-empty text, 0 for an empty one.
- They do not count the net change. A whole-line insertion `x\n` at a line start is 0 -> 2, and a whole-line deletion is 2 -> 0.
- The semantics and the r2 tests (`test_a_window_replaced_by_fewer_lines_states_the_lines_and_bytes_replaced`, `test_the_line_count_is_stated_in_the_file_s_own_bytes`, `test_a_replace_that_keeps_the_line_count_adds_nothing_to_the_echo`) are unchanged and green in the suite.

## Whole suite

| tree | result | bare RC | log | import proof |
|---|---|---|---|---|
| release base `101da5c` | 1209 passed, 3 skipped | PYTEST_RC=0, DOCKER_RC=0, SUITE_BASE_RC=0 | `suite_base.txt` (`TREE_HEAD=101da5c… DIRTY_PATHS=0`) | `IMPORT_PROOF /home/linxuhao/stepflow-coords-r1-base/src/skillflow/__init__.py` |
| candidate | 1409 passed, 3 skipped | PYTEST_RC=0, DOCKER_RC=0, SUITE_CAND_RC=0 | `suite_candidate.txt` | `IMPORT_PROOF /home/linxuhao/stepflow-coords-r1/src/skillflow/__init__.py` |

- 1409 = r2's 1243 + the 166 new tests. No new red.
- The candidate suite ran on the working tree before the commit (`TREE_HEAD=6879e9d… DIRTY_PATHS=5`). The sha256 it logged for the changed sources are:
  - `strict_patch.py` `31ce5f37…`
  - `citations.py` `ca6ea7db…`
  - `read_tools.py` `ae3f9703…` (unchanged)
  - `tool.yaml` `bedc01e2…`
- `committed_hashes.txt` hashes the same four files as committed (`git show HEAD:<path> | sha256sum`), for comparison.

## Word-diff read-back

`worddiff_readback.txt` is `git diff --cached --word-diff 6879e9d` for every changed non-log file:
- the 3 sources;
- the new test file;
- the scripts;
- this note.

The logs are excluded. Splice check:
- I read every hunk back: no duplicated adjacent lines, fused lines, cut sentences or stale tails.
- An awk scan for identical adjacent non-blank lines finds 0 hits in all 12 changed non-log files: the sources, the test file, the scripts and this note (`splice_scan.txt`).
- The self-defence phrases (`not a defect`, `by design`, `documented`, `known limitation`, `intentionally`) appear 0 times in added lines (`splice_scan.txt`); in this note only on this line, which lists them.

## Not done, stated plainly

- **A window on old line 1 after this run inserts at the top of the file is refused** although the line survived. This is 18 of the census's 31 false refusals.
  - The top insertion is journaled at offset 0, and `remap` keeps an offset at an edit's start in place. So the window then spans the inserted text and its own line, fails the text check and is refused.
  - An insertion point at 1:0 issued before such an edit lands BEFORE the inserted lines, by the same rule. The sweep pins that position.
- **A blank-line window next to this run's edit is refused** even when the line survived. This is the other 13 false refusals: e.g. a blank line right after a deleted line, or right before an insertion.
  - `touched` cannot tell which side of an edit a zero-width range belonged to, so it refuses both.
  - The safe half, deleted blank lines refused, is what the new check buys. The false half is its cost.
- **Insertion points after a deletion are compared across formats only.** The 58 point probes on deletion edits are checked to be equal between V4A and reference; the place they land is not checked against an oracle. The tie rule places them, and no test pins that position.
- **An insertion's neighbours are not re-checked** (unchanged from r2). An empty span covers no text, so an own edit that starts or ends exactly at an insertion point is not refused. `remap`'s tie rule places the point.
- **The sweep covers single-block edits only.** It uses 6 shapes × every position × 3 texts, one hunk each.
  - Multi-hunk V4A patches and hunks with several changed blocks are journaled by the same `_line_blocks` code: consecutive hunks with no kept line between them merge into one block.
  - No sweep compares those against references. The existing multi-hunk tests in the suite pass.
- **H7 follows the hunk's literal rows.** When a hunk's context line is text the author meant as some other copy, the journal records what the hunk says, and a citation of that line follows it. See H7 above; per the goal it is not counted.
- **A `Hunk` built in code without `rows`** (not through `parse_patch`) is journaled as one block: all its old lines replaced by all its new ones. Citations inside it are refused rather than translated.
- **`_spans_account_for` checks that the recorded spans are a reading of the edit, not that they are the applier's reading.** The applier is the only source of which reading; the guard catches spans that describe some other edit entirely.
- **A citation issued with no frame** (`citations.issue` without `start_char`) is still judged by the text at its line numbers (unchanged from r2). No read issues one.
- **Resending a preview without looking at it still writes what the count covered** (`A1'` in `replay_candidate.txt`: `= 450`). This is unchanged from r1 and r2.
- **Explicit lines-only ranges are not corroborated** (unchanged).
- **`~/AItelier/templates/coding_impl.md`** is still out of scope. Its replacement text is in `evidence/coords-r1/delivery_notes.md`.
- No tag, version bump, build or publish, and no State DAG write.
