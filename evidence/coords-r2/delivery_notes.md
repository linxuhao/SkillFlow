# editing.the-coordinates-must-come-from-the-read-too (rev 3) — delivery notes

- Attempt `attempt-9c2c6cc8d8174700847f0f45e4466983`, branch `director/coords-r2-20260924`.
- Base: the r1 candidate `382664c0c966eaf7ec9d1456af43a6e19afcdcec`. Release base: `101da5c44e23aadbb0dcf3a423a0ac43286453bc` (1.5.79).
- The r1 review is `~/.AItelier/director/reports/coords-r1-review-20260924/review.md` (sha256 `86f7316f…`), called `$R` below.
- Written 2026-09-24 (UTC). Every log named here is in this directory (`evidence/coords-r2/`) as `.txt`, unless it names `$R`.
- Every run used a throwaway `docker run --rm --init -m 3g aitelier:latest`, with the tree's `src/` first on `PYTHONPATH`. Each log prints `IMPORT_PROOF <tree>/src/skillflow/__init__.py`, and every RC below was captured bare (`$?`, no pipe).

## The director's ruling this round follows (quoted)

> Director ruling: take reading (1).
>
> In the rev 3 wording, 「文件在它签发之后被改过」 means changed by anything other than this run's own journaled edits, or the shown bytes themselves were edited. The run's own edits above the span are known exactly, and they are translated, not refused. That matches the property the goal states first: 一个 citation 只能写到它签发时展示给调用方的那个位置.
>
> What must hold, each with a test:
>
> - **E and S3 (the review's cases, verbatim):** the old preview writes exactly the bytes the review's E0 control produced, at the place it was shown, never at the re-issued place. Pin those bytes.
> - **T1 (the shown text was edited after the preview):** refused, with a "reread / re-preview" message, and nothing written.
> - **T2 (the file was written from outside the run after the preview):** refused, for spans AND windows. You say the strict fallback lets the window shape through today, so close that too.
> - **M3 and M3b:** killed literally by T1 and T2, with bare RC 1 and ignition > 0.
> - **Every field that decides where a sha resolves is covered by the sha.** Show that a re-issue at the same coordinates after an own edit yields a different sha, or an equivalently bound one that cannot resolve elsewhere.

## What changed

1. `src/skillflow/citations.py`
   - **The digest covers the frame.** Window and span digests are now HMACs over the run, path, source, lines, columns (spans) and text, as before, plus `_frame(epoch, generation, file_sha, start_char)`.
     - `epoch`: which journal chain the range was issued in. It is new, unique per chain, and taken from a process-wide counter.
     - `generation`: the version inside that chain.
     - `file_sha`: the digest of the whole normalised text the range was issued against.
     - `start_char`: where the range starts in that text.
   - These are every field the resolution reads. The coordinates and text were already covered; the frame fields pick the journal and offset the coordinates are translated from.
   - The digest is computed under the lock, after the generation is known. `matches()` recomputes it with the record's own frame.
   - **The journal chain has an identity.** Every chain gets an `epoch`, and `chain_intact(..., epoch=)` refuses a citation from any other chain.
     - A chain is replaced whenever the file is found at a version the journal did not write (an outside write, `break_journal`, eviction). Generation numbers restart at 0 in the new chain.
     - Before this, a citation from the old chain could be matched against the new one whenever the generation and the text happened to coincide.
2. `src/skillflow/strict_patch.py`
   - `_span_range` passes the epoch to the chain check. The stale message now names both causes and the remedy: `…changed since its citation was issued: the text it showed was edited, or the file was written by something other than this run's apply_patch; reread the range and resend the reference with the new digest to be shown the text it covers now`.
   - **A framed window whose chain is broken is refused** (`…changed since the digest was issued, by a write this run's journal does not account for (…); reread the range and cite the new digest`).
     - Before this, such a window fell back to comparing the text at the cited line numbers. That comparison passes when an outside write shifts identical lines, and it then wrote at a line the read never showed (T2-window, below).
     - Only a citation issued with no frame (`citations.issue` called without `start_char`, which no read does) is still judged by line numbers.
   - **The echo states a line-count change.** When a reference's replaced text and its `new_text` span different numbers of lines, its `replaced` entry adds:
     - `replaced_lines`;
     - `replaced_bytes`, in the file's own bytes, CRLF included;
     - `new_lines`.
     There is no heuristic and no refusal. Entries whose line count is unchanged are byte-identical to r1's, so the 73 tests' exact-dict assertions are untouched.
3. `src/skillflow/tools/apply_patch/tool.yaml`
   - Says a sha writes only where it showed the caller: its own writes are translated, an edit of the covered text or an outside write gets it refused.
   - Names the three new echo fields.
4. `tests/test_a_citation_writes_only_where_it_was_shown.py` (13 tests, new). Each review case is copied from the probe named in its docstring (`$R/probe.py`, `$R/probe2.py`, `$R/probe3.py`). Only the helpers were adapted to a pytest fixture: the run id and the tmp dir.

## Route and discriminating reason (criterion 1)

**The route is unchanged from r1: caller-supplied digest redundancy.** The caller quotes a digest the engine issued, and never a count or the original text.
- Explicit columns must cite a span the engine issued for exactly those coordinates, after showing the covered text.
- The common intent needs no column at all: the whole window (sha only) or whole lines (lines only).

**This round's reason, why the fix is binding the frame into the digest:**
- A digest resolves to a place by reading exactly these fields: coordinates, text, epoch, generation, file_sha, start_char. All of them are now inside the HMAC.
- So two issues share a sha only when they are equal in every field the resolution reads. When they are, they resolve to the same place by construction.
- A later issue therefore cannot move an earlier sha. It gets its own sha, and the earlier one still resolves to the place it showed, translated through this run's own journaled edits.

**Rejected alternatives:**
- **Refuse a re-issue that would move an existing sha.** Rejected because the re-issue is a read or a preview, which is how a caller recovers. Refusing it would block the recovery path, not the wrong write.
- **Refuse every citation after any change.** Rejected by the ruling, since own edits are translated. It would also turn the frozen-frame tests red (`test_a_second_edit_below_the_first_needs_no_read_in_between` and siblings).

**Why two separate checks still decide T1 and T2:**
- The frame binding fixes where a sha points. It does not by itself refuse T1 or T2: in both, the sha still points at the right frame.
- T1 is refused only by the text comparison, and T2 only by the chain check.
- The mutants below show each of those checks is the one that decides.

## a-mis-specified-range-cannot-write-silently

### Both poles, re-measured on this tree

**Pole 1: the len-1 call is refused and writes nothing.** Source: `replay_candidate.txt` part C (the director's probe shape), SCRIPT_RC=0, DOCKER_RC=0. The `to_col = len-1 (0..27)` call gives `applied=False`, line 3 unchanged `DEFAULT_TIMEOUT_SECONDS = 30`, and:

    a column the caller counted has nothing to check it against, so columns must cite a span citation issued for exactly those coordinates. Nothing was written. reference 1 names columns 3:0..3:27, which cover 'DEFAULT_TIMEOUT_SECONDS = 3' and leave '' before it and '0' after it on their lines; …

**Pole 2: len+1 is unchanged, word for word.** Same log, `to_col = len+1 (0..29)`:

    to column 29 is past the end of line 3 (28 characters); reread the range

On base the len-1 call writes `= 450` (`replay_base.txt`, SCRIPT_RC=0).

The review's full probe (`$R/probe.py`, unmodified) was re-run on this tree: `probe_candidate.txt`, DOCKER_RC=0, 54 CASE blocks. `probe_diff_vs_review_r1.txt` is its diff against the review's r1 run, with 40-hex shas and tmp dirs masked. Only three kinds of line differ:
- the import path;
- one random never-issued sha prefix;
- the stale message wording (3 lines).

Two outcomes also changed, S3 and S3w, which now write the intended bytes. Every other case, including P1, P3, P4, S1a–f, S2a–d, S4a–d and S5–S11, prints the same outcome as on r1.

### The review's cases, replayed verbatim

Each case below is a test in `tests/test_a_citation_writes_only_where_it_was_shown.py`, and was also re-run as the review's own probe script, unmodified.

| case (source) | shown place | this tree | log | r1 (review) |
|---|---|---|---|---|
| E (`$R/probe2.py`) | insertion before `l3` | `span_a == span_b: False`; `WROTE=True applied=True`; actual `b'l1\nNEW1\nNEW2\nl2\n# note\nl3\nl4\nl5\n'` = E0's bytes, `CORRECT=True` | `probe2_candidate.txt`, DOCKER_RC=0 | wrote before `NEW2`, CORRECT=False |
| E0 control (`$R/probe2.py`) | same | same bytes, `CORRECT=True` | same | CORRECT=True |
| S3 (`$R/probe.py`) | frame-A line 2, now line 4 | `span_a == span_b: False`; `line 4 = 'x = 45'`; actual `b'a = 1\nx = 30\nb = 2\nx = 45\nb = 2\nx = 30\nc = 3\n'` | `probe_candidate.txt`, DOCKER_RC=0 | wrote line 2 |
| W (`$R/probe2.py`, window) | frame-A line 2, now line 4 | `wa == wb: False`; `CORRECT=True`, same bytes as S3's intended | `probe2_candidate.txt` | CORRECT=False (on base too) |
| S3w (`$R/probe.py`, window) | same | `wa == wb: False`; actual = intended | `probe_candidate.txt` | wrote line 2 |
| T1 (`$R/probe3.py`) | `DEFAULT_TIMEOUT_SECONDS = 3`, then edited inside by this run | `WROTE=False applied=False`, error `span 3:0..3:27 changed since its citation was issued: the text it showed was edited, or the file was written by something other than this run's apply_patch; reread the range and resend the reference with the new digest to be shown the text it covers now` | `probe3_candidate.txt`, DOCKER_RC=0 | refused (old wording) |
| T2 span (`$R/probe3.py`) | `30` on line 3 of 5 identical lines, then an outside write adds one on top | `WROTE=False applied=False`, same error for `span 3:4..3:6` | `probe3_candidate.txt` | refused (old wording) |
| T2 window (new) | the same file shape, window cited with lines 3..3 | refused: `…changed since the digest was issued, by a write this run's journal does not account for (…); reread the range and cite the new digest` | test `test_T2_a_window_after_an_outside_write_is_refused` | **wrote line 3** |

The tests pin these bytes. `E_SHOWN_PLACE` is the E0 bytes above, and `DUP_SHOWN_PLACE` is S3/W/S3w's intended product.

**The sha covers the fields that decide its place.** `test_the_same_range_in_the_same_version_is_one_sha_and_in_another_is_two` checks four things:
- the same window read twice in one version gives one sha;
- the same span previewed twice in one version gives one sha;
- after this run's own edit BELOW the range, the same lines give a different sha. The text and offset are unchanged there; only the version moved.
- the same holds for the span.

`test_a_span_from_before_an_outside_write_stays_refused_after_the_run_rebuilds_the_same_text` covers the chain identity. An outside write intervenes, then this run's own edits rebuild exactly the text and generation the span was issued in. The span is still refused.

**Both polarities on the r1 tree.** The new test file run against r1's `src/` (`~/stepflow-coords-r2-prev`, detached at 382664c) gives `new_tests_on_r1.txt`: `11 failed, 2 passed`, PYTEST_RC=1, DOCKER_RC=1.
- The 2 that pass on r1 are the E0 control and the unchanged-line-count echo, both of which r1 already did right.
- On r1, E, S3, W, S3w and the same-version test fail on `assert span_a != span_b` / `wa != wb`: r1 gives equal shas.
- T2-window and the rebuilt-chain test fail on `assert True is False`: r1 applied the write.
- T1 and T2-span fail only on the word `reread`: r1 already refused them, with the old wording.
- The two echo tests fail with `KeyError: 'replaced_lines'`.

### M3 and M3b, and the new checks, killed on disk

Each mutant runs on a throwaway copy of the tree (`tar` of the worktree without `.git`, into `~/coords-r2-scratch/mut_<m>`).
- Whole suite per mutant, bare RC.
- An IGNITION is logged each time the ORIGINAL code would have refused (for M7: each time an issue re-used a sha for a different frame) and the mutant did not.
- Driver: `mutants.sh`. Output: `mutants_run.txt`, DRIVER_RC=0.
- Mutant code: `mutate_r2.py`. M3 uses the review's own `$R/mutate.py`, unmodified.
- Diffs: `mutant_<m>.diff.txt`. Ignition logs: `mutant_<m>_ignitions.txt`.

| mutant | change | whole suite (log) | bare RC | ignitions | red tests |
|---|---|---|---|---|---|
| control | none | 1243 passed, 3 skipped (`mutant_control_suite.txt`) | SUITE_control_RC=0 | 0 | none |
| **M3** | the review's M3, verbatim: the span text comparison dropped | 1 failed, 1242 passed (`mutant_m3_suite.txt`) | **SUITE_m3_RC=1** | **1** | `test_T1_the_shown_text_edited_after_the_preview_is_refused` |
| **M3b** | the review's M3b: the span chain check's `raise stale` replaced by an ignition | 2 failed, 1241 passed (`mutant_m3b_suite.txt`) | **SUITE_m3b_RC=1** | **3** | `test_T2_a_span_after_an_outside_write_is_refused`, `test_a_span_from_before_an_outside_write_stays_refused_after_the_run_rebuilds_the_same_text` |
| M5 | the epoch comparison in `chain_intact` removed | 1 failed, 1242 passed (`mutant_m5_suite.txt`) | SUITE_m5_RC=1 | 1 | `test_a_span_from_before_an_outside_write_stays_refused_after_the_run_rebuilds_the_same_text` |
| M6 | the framed-window refusal on a broken chain removed (back to r1's line-number check) | 1 failed, 1242 passed (`mutant_m6_suite.txt`) | SUITE_m6_RC=1 | 5 | `test_T2_a_window_after_an_outside_write_is_refused` |
| M7 | the frame dropped from the digest (r1's digest) | 5 failed, 1238 passed (`mutant_m7_suite.txt`) | SUITE_m7_RC=1 | 6 | E, S3, W, S3w, same-version tests |

**M3b, "verbatim".** The review's `$R/mutate.py m3b` no longer applies to this tree: `MATCHES 0 for m3b`, `MUTATE_m3b_review_RC=2` in `mutants_run.txt`. The chain-check call it matches gained one argument line (`epoch=record.get("epoch")`). `mutate_r2.py`'s `m3b` is the same mutation, `raise stale` to an ignition, with that one line added to its match string.

Coverage, as a cross product:
- 5 mutants (M3, M3b, M5, M6, M7) plus an unmutated control × the whole suite (1243 tests), 1 run each.
- Every mutant is killed.
- Each named check has at least one test that fails when it alone is removed.

## prove-it-on-the-edit-that-actually-went-wrong

Re-measured on this tree, with the r1 script unmodified (`evidence/coords-r1/replay_real_edits.py`). Logs: `replay_candidate.txt` (SCRIPT_RC=0, DOCKER_RC=0) and `replay_base.txt` (base, SCRIPT_RC=0, DOCKER_RC=0).

**The file and the read.**
- Same file: fixture sha256 `036440eb37d4428cd72f43650cdb1750abdddd86f67b026610afa941fdac0ffe`, 49,447 bytes.
- `read(1674,1685)` reproduces trace id 17's window: `start_line 1675 end_line 1684 end_col 19 start_byte 49155 end_byte 49447`.

**The edits:**
- **A1**, trace id 22 verbatim (`to_col=27`): `applied=False`, line 1679 `'DEFAULT_TIMEOUT_SECONDS = 30'`, `A1 file byte-identical to the original: True`. On base: `applied=True`, `'DEFAULT_TIMEOUT_SECONDS = 450'`.
- **A2**, same read, same line, no columns: `applied=True`, `line 1679 = 'DEFAULT_TIMEOUT_SECONDS = 45'`, `A2 rest of file byte-identical: True`.
- **A3**, sha + new_text only, from `read(1678,1679)`: `applied=True`, `line 1679 = 'DEFAULT_TIMEOUT_SECONDS = 45'`.

**wuxia b3983d63 (second case):**
- B1 (`to_col=58`) is refused, showing `'\t"res://tests/" + "test_martial_arts_surface_census" + ".g'` with `'d",'` left behind; `B1 file == pre-edit bytes: True`.
- B2 and B3 write `'\t"res://tests/test_martial_arts_surface_census.gd",'`.

**The 73 reference-mode tests** (`test_reference_hunks` + `test_strict_patch_parser` + `test_output_targets`):
- candidate: `73 passed`, PYTEST_RC=0, DOCKER_RC=0 (`reference73_candidate.txt`);
- base: `73 passed`, PYTEST_RC=0, DOCKER_RC=0 (`reference73_base.txt`).

**Byte identity of the 8 tests r1 rewrote**, measured by running the real test functions:
- Method: the review's `run_bytes.sh` + `$R/hash_tmp.py` (copy: `run_bytes.sh`, output path moved to the scratch dir). The base tree runs the 8 original tests and this tree runs the 8 rewritten ones, and every `.py` each left in its tmp dir is hashed.
- Runs: base `8 passed`, PYTEST_RC=0, DOCKER_RC=0 (`rewritten_real_base.txt`); candidate `8 passed`, PYTEST_RC=0, DOCKER_RC=0 (`rewritten_real_candidate.txt`).
- Result: `IDENTICAL 22 / 22 (candidate keys 22)`, COMPARE_RC=0 (`rewritten_real_compare.txt`; hashes in `rewritten_real_*.json.txt`).
- The 22 are 11 distinct files, each seen twice through pytest's `current` symlink: one per test, except 2 for the v4a test and 3 for the unordered test.

No docstring or message attributes the eac7cacb error to the model.

## replace-what-was-read-without-naming-a-column

**Pole 1: no column number.** `replay_candidate.txt` part C, `sha + new_text only (read line 3)`: `applied=True`, product bytes `b'import os\n\nDEFAULT_TIMEOUT_SECONDS = 45\n\nDEBUG = False\n'`. Test: `test_citing_the_sha_alone_replaces_the_line_that_was_read` in `tests/test_the_coordinates_come_from_the_read.py`, 34 passed with the new file (`new_tests_candidate.txt`, PYTEST_RC=0, DOCKER_RC=0).

**Pole 2: a narrowed sub-range still works.** Part C `no columns (lines 3..3)` gives `= 45`. The column cut `3:26..3:28 -> '45'` is refused on the first call, showing `'30'`, and written by the span on the second (`$R/probe.py` P6a/P6b, unchanged in `probe_diff_vs_review_r1.txt`). The two forms give the same bytes.

**The review's new risk (S6e): a window replaced by fewer lines now says so.** No heuristic was added.
- When a reference changes the line count, its `replaced` entry adds `replaced_lines`, `replaced_bytes` (in the file's own bytes) and `new_lines`.
- `test_a_window_replaced_by_fewer_lines_states_the_lines_and_bytes_replaced` uses eac7cacb's own 10-line window `read(1674,1685)`, cited by sha alone with one line of `new_text`. The result carries `replaced_lines == 10`, `replaced_bytes ==` the window's UTF-8 length, and `new_lines == 1`.
- `test_the_line_count_is_stated_in_the_file_s_own_bytes`: on CRLF, `(3, 7, 1)`.
- `test_a_replace_that_keeps_the_line_count_adds_nothing_to_the_echo`: an unchanged count leaves the entry exactly as r1 shaped it.

## Whole suite

| tree | result | bare RC | log | import proof |
|---|---|---|---|---|
| release base `101da5c` | 1209 passed, 3 skipped | PYTEST_RC=0, DOCKER_RC=0, SUITE_BASE_RC=0 | `suite_base.txt` (`TREE_HEAD=101da5c… DIRTY_PATHS=0`) | `IMPORT_PROOF /home/linxuhao/stepflow-coords-r1-base/src/skillflow/__init__.py` |
| candidate | 1243 passed, 3 skipped | PYTEST_RC=0, DOCKER_RC=0, SUITE_CAND_RC=0 | `suite_candidate.txt` | `IMPORT_PROOF /home/linxuhao/stepflow-coords-r1/src/skillflow/__init__.py` |

- 1243 = r1's 1230 + the 13 new tests. No new red.
- The candidate suite ran on the working tree before the commit (`DIRTY_PATHS=5`). The sha256 of the four changed sources it logged are:
  - `strict_patch.py` `ecd68fde…`
  - `citations.py` `0c2a05c1…`
  - `read_tools.py` `ae3f9703…` (unchanged)
  - `tool.yaml` `3ee6532b…`
- These equal the committed files, and `cmp` shows them identical to the mutant control copy.

## Word-diff read-back

`worddiff_readback.txt` is `git diff --word-diff 382664c` for every changed non-log file:
- the 3 sources;
- the new test file;
- the 6 runner and mutant scripts;
- this note.

The logs are excluded. Splice check:
- I read every hunk back: no duplicated adjacent lines, fused lines, cut sentences or stale tails;
- an awk scan for identical adjacent non-blank lines in the changed source and test files finds 0 hits (`splice_scan.txt`).

The self-defence phrases (`not a defect`, `by design`, `documented`, `known limitation`, `intentionally`) appear 0 times in added lines (`splice_scan.txt`).

## Not done, stated plainly

- **A citation issued with no frame** (`citations.issue` without `start_char`) is still judged by the text at its line numbers, so the T2 shape would still write for it. No read issues one: every citable read passes `start_char` and `file_sha` (`read_tools.py:107-112`). Only direct callers of `citations.issue`, such as tests, can.
- **Resending a preview without looking at it still writes what the count covered** (`A1'` in `replay_candidate.txt`: `= 450`). This is unchanged from r1 and follows the two-call design of the rev 2 ruling.
- **Explicit lines-only ranges are not corroborated.** This is unchanged from r1.
- **An insertion's neighbours are not re-checked.** An empty span covers no text, so the text check has nothing to compare. Suppose this run's own edit starts or ends exactly at the insertion point: it changes what `keeps_before`/`keeps_after` showed, and the insertion is not refused. It lands by `remap`'s boundary rule: an offset at an edit's start stays put, one at its end moves past the new text. No test pins that adjacent case. E and E0 pin only an edit elsewhere in the file.
- **`~/AItelier/templates/coding_impl.md`** is still out of scope. Its replacement text is in `evidence/coords-r1/delivery_notes.md` and is unaffected by this round.
- No tag, version bump, build or publish, and no State DAG write.
