# editing.the-coordinates-must-come-from-the-read-too (rev 2) — delivery notes

Attempt `attempt-684a5e14cc2e4a638de437aff4b24961`, branch `director/coords-r1-20260924`,
base `101da5c44e23aadbb0dcf3a423a0ac43286453bc` (release 1.5.79). First commit `d8c490a`;
this note describes the amended candidate, the commit on top of it that follows the
director's ruling of 2026-09-24 (~13:50Z). Written 2026-09-24 (UTC).
Evidence convention: this directory (`evidence/coords-r1/`); the repo had no delivery-note
convention, only `evidence/artifact-revision-20260912/` wheel dumps. Every log is `.txt`.
The logs replaced in the amendment are in `d8c490a` (git history), not here.

## What changed

1. `src/skillflow/strict_patch.py`
   - A reference may omit `from_line`/`to_line`: `{file, sha, new_text}` replaces exactly
     the window the citation covers, every coordinate taken from the citation (d8c490a).
     Half a range is refused.
   - **A reference that names a column is refused unless its `sha` is a span citation
     issued for exactly those coordinates.** The refusal writes nothing. It is the preview:
     for each such reference, the error and the result's `spans` give the exact text those
     coordinates cover, what they leave on each side (`keeps_before`/`keeps_after`), and a
     span sha. Resending the reference with that sha writes. At write time the engine checks
     the span sha names these same coordinates, and that they still cover the text it
     showed; either mismatch is refused. Every other refusal (outside the window, past the
     end of the line, stale, replaced, overlapping) keeps its message and comes first.
   - A span sha may also be cited with no coordinates (`{file, sha, new_text}`), meaning the
     span.
2. `src/skillflow/citations.py` — `issue_span`: an HMAC over the named coordinates and the
   covered text, recorded per run with its place in the file and the file's journal
   generation. An edit elsewhere in the file does not stale it; a change under it does.
3. `src/skillflow/tools/apply_patch/tool.yaml` — the column form is documented as a two-call
   edit (preview, then cite the span). `src/skillflow/read_tools.py` — the read description
   names the sha-only form (d8c490a).
4. Tests: `tests/test_the_coordinates_come_from_the_read.py` (21 tests), the fixture
   `tests/fixtures/eac7cacb_app_settings.py.txt`, and 8 rewritten tests (table below).

## The discriminating reason (criterion 1)

A column is a number the caller counted, and nothing in the number says whether the count
was right. The only way to have a column written is now to be shown, by the engine, the
exact characters those coordinates cover and what they leave on the line (for eac7cacb:
`DEFAULT_TIMEOUT_SECONDS = 3`, with `0` left behind), and to send back the span citation
the engine issued for exactly that range. The write is bound to those characters. So a
caller that miscounts a column cannot write without first being shown the miscount's own
result: its first call writes nothing and returns that text. And the common intent — the
whole line, the whole window — needs no column at all.

This is caller-supplied digest redundancy. The digest is issued by the engine, because the
caller cannot compute one (the key is private to the process). It is not the forbidden
non-fix (a): nothing lets the caller count better. It is not (b): the caller never retypes
the original, and a reference still has no field that could hold it
(`test_a_reference_hunk_has_nowhere_to_put_the_original` is unchanged and green).

## a-mis-specified-range-cannot-write-silently

Pole 1 — the len-1 mistake. As sent, it is refused and nothing is written
(`test_the_old_uncorroborated_column_call_is_refused_with_the_new_error`, strict path and
real-read path). In the column-free writing it writes `= 45`
(`test_the_len_minus_one_belief_has_no_field_to_travel_in`). From `replay_candidate.txt`
part C (the director's probe shape), SCRIPT_RC=0, DOCKER_RC=0:

    to_col = len-1 (0..27)   applied=False   line 3 = 'DEFAULT_TIMEOUT_SECONDS = 30'
    error: apply_patch preflight: app/settings.py: a column the caller counted has nothing to check it against, so columns must cite a span citation issued for exactly those coordinates. Nothing was written. reference 1 names columns 3:0..3:27, which cover 'DEFAULT_TIMEOUT_SECONDS = 3' and leave '' before it and '0' after it on their lines; if that is exactly the text to replace, resend this reference with sha ac8ffd6a58b1050c02263d3b493022da350250c5 (in `spans`). To replace whole lines instead, omit from_col and to_col.
    sha + new_text only (read line 3)   applied=True   line 3 = 'DEFAULT_TIMEOUT_SECONDS = 45'
    product bytes b'import os\n\nDEFAULT_TIMEOUT_SECONDS = 45\n\nDEBUG = False\n'

A correct count is refused the same way (`to_col = len (0..28)`, and
`test_a_counted_whole_line_is_a_counted_column_too`). 28 is as unchecked as 27; the whole
line is asked for by omitting the column.

Pole 2 — len+1 is still loud, word for word, and is not widened into anything else:

    to_col = len+1 (0..29)   applied=False
    error: apply_patch preflight: app/settings.py: to column 29 is past the end of line 3 (28 characters); reread the range

(`test_the_len_plus_one_error_is_still_loud_and_word_for_word` also pins the real-read
form `... as the read served it (28 characters)`.)

Binding checks: a span sha cited with other coordinates is refused, in 4 shapes
(`test_a_span_citation_corroborates_only_its_own_coordinates`). A span whose text changed
after the preview is refused (`test_a_span_is_bound_to_the_bytes_it_showed`). A span
survives an edit elsewhere in the file, and its sha alone is enough
(`test_a_span_survives_an_edit_elsewhere_and_its_sha_alone_is_enough`).

Mutation 3 — the corroboration check deleted (`mutation3_patch.py.txt`, a throwaway copy):
16 failed, 91 passed, PYTEST_RC=1, DOCKER_RC=1, IGNITIONS=16
(`mutation3_corroboration_deleted.txt`). The 16 red tests are named in the log: the 8
rewritten tests, 7 new corroboration tests and the narrowed-sub-range test.

## prove-it-on-the-edit-that-actually-went-wrong

Source: host run eac7cacb, `/tmp/refmode-3r_9rvh9/sf.db` trace ids 16/17 (read) and 22/23
(apply_patch), copied to `eac7cacb_trace_16_17_22_23.txt`. The file is
`/tmp/refmode-3r_9rvh9/repo` HEAD `1674e5f` `app/settings.py`: blob
`a1f2b73a72018708afc0230a05f177d478802158`, sha256
`036440eb37d4428cd72f43650cdb1750abdddd86f67b026610afa941fdac0ffe`, 49,447 bytes. The
committed fixture has the same blob id and sha256, and the test asserts the sha256. The
replay issues the same read (`start_line=1674, end_line=1685`) and gets trace id 17's window
(`1675-1684, end_col 19, bytes 49155-49447`). From `replay_candidate.txt`:

    A1 trace id 22 verbatim (to_col=27)   applied=False   line 1679 = 'DEFAULT_TIMEOUT_SECONDS = 30'
    error: apply_patch preflight: app/settings.py: a column the caller counted has nothing to check it against, so columns must cite a span citation issued for exactly those coordinates. Nothing was written. reference 1 names columns 1679:0..1679:27, which cover 'DEFAULT_TIMEOUT_SECONDS = 3' and leave '' before it and '0' after it on their lines; if that is exactly the text to replace, resend this reference with sha 3e56fdf6ba5d1b868d543dc604ec1515067e18a4 (in `spans`). To replace whole lines instead, omit from_col and to_col.
    A1 file byte-identical to the original: True
    A2 same read, same line, no columns   applied=True   line 1679 = 'DEFAULT_TIMEOUT_SECONDS = 45'
    A2 rest of file byte-identical: True
    A3 sha + new_text only                applied=True   line 1679 = 'DEFAULT_TIMEOUT_SECONDS = 45'

On base the same A1 call writes `DEFAULT_TIMEOUT_SECONDS = 450` (`replay_base.txt`).
Tests: `test_host_run_eac7cacb_exact_call_is_refused_not_written`,
`test_host_run_eac7cacb_replayed_writes_the_line_it_meant`,
`test_host_run_eac7cacb_replayed_with_the_sha_alone`.

Second real case (not a substitute): wuxia run b3983d63, trace ids 2340/2341 (read) and
2354/2355 (`to_col=58` on a 61-character line); commit `5ac8714e` holds the result (blob
`0eae2c14…`, copied to `wuxia_5ac8714e_unit_test_runner.gd.txt`). The replay reproduces trace
id 2341's served window (`True`). The verbatim call is now refused. It shows
`'\t"res://tests/" + "test_martial_arts_surface_census" + ".g'` with `'d",'` left after it,
and the file stays at its pre-edit bytes (`B1 file == pre-edit bytes: True`). The column-free
forms write `\t"res://tests/test_martial_arts_surface_census.gd",` (B2, B3). On base, B1
reproduces commit 5ac8714e byte for byte.

The 73 delivered reference-mode tests (`test_reference_hunks` + `test_strict_patch_parser`
+ `test_output_targets`): candidate 73 passed, PYTEST_RC=0, DOCKER_RC=0
(`reference73_candidate.txt`); base 73 passed, PYTEST_RC=0 (`reference73_base.txt`). Five
of the 73 were rewritten (table below).

## replace-what-was-read-without-naming-a-column

Pole 1 — `test_citing_the_sha_alone_replaces_the_line_that_was_read`: the 28-character line,
read alone, cited as `{file, sha, new_text}` (asserted to be exactly those three keys). The
bytes are `b'import os\n\nDEFAULT_TIMEOUT_SECONDS = 45\n\nDEBUG = False\n'` (the same bytes
are printed in `replay_candidate.txt`).

Pole 2 — `test_a_narrowed_sub_range_is_still_honoured_as_written`: whole lines inside a
wider window (`from_line=3,to_line=3`, one call), and a column cut `3:26..3:28` → `45` (two
calls: the preview shows text `30`, keeps_before `DEFAULT_TIMEOUT_SECONDS = `, keeps_after
empty, then the span is cited). Both produce the same bytes as pole 1. The sub-line cut
survives in all 8 rewritten tests, with their output bytes unchanged (below).

## Rewritten tests

Each rewritten test now sends its original call, asserts that call is refused with
`applied=False`, `written=[]`, `partial=False` and the file unchanged, then resends citing
the returned spans. Every output assertion line is unchanged from base; only the call site
changed (see `worddiff_readback.txt`). Byte identity was measured independently:
`rewritten_tests_bytes.py` replays each test's column calls on the base tree (original call)
and on the candidate (original call, then the resend), and compares the final file bytes.
Result: `IDENTICAL 8/8`, COMPARE_RC=0 (`rewritten_bytes_compare.txt`; both runs
SCRIPT_RC=0, DOCKER_RC=0, `rewritten_bytes_base.txt`, `rewritten_bytes_candidate.txt`).
The candidate log prints the new refusal for every original call.

| test | original call (before) | after | asserted output (unchanged) | final bytes sha256 (base = candidate) |
|---|---|---|---|---|
| test_reference_hunks::test_a_digest_the_read_issued_applies (of the 73) | `apply(... 2:0..2:13 ...)` → applied | `apply_corroborated(...)`; preview shows `'    value = 1'` | file == BODY with `value = 1`→`value = 41` | 32689e36… |
| test_reference_hunks::test_reference_and_v4a_produce_the_same_bytes (of the 73) | `apply(... 2:0..4:16 ...)` | `apply_corroborated(...)`; preview shows the three lines | ref bytes == V4A bytes | 4f2ec012… |
| test_reference_hunks::test_one_word_cites_one_line_and_leaves_the_rest_alone (of the 73) | `apply(... 7:11..7:12 "7")` | `apply_corroborated(...)`; preview `'0'` | `return 0`→`return 7` | a2485f03… |
| test_reference_hunks::test_unordered_references_match_descending_ones_byte_for_byte (of the 73) | `apply(3 cuts, 3 orders)` | `apply_corroborated(...)`; 3 spans per order | desc == scrambled == asc, 3 substrings, line count | bb622c35… |
| test_output_targets::test_reference_mode_runs_end_to_end_through_the_engine (of the 73) | `call(... 'apply_patch' ...)` ×2 through the engine | `patch_corroborated(...)` through the engine | `'answer = 11111\nother = 22\nlast = 33\n'`, third echo `'2'`, "gone" still `changed since the digest was issued` | 3abb78fa… |
| test_a_successful_write::test_citing_inside_a_span_this_run_replaced_is_refused_by_name | `apply(... 3:4..3:10 "SECOND")` | `apply_corroborated(...)`; preview `'second'` | second call refused `replaced by an earlier edit in this run` | 112556c5… |
| test_a_successful_write::test_the_result_says_what_it_replaced | `apply(... 2:4..2:9 ...)` | `apply_corroborated(...)`; preview `'first'` | echo `{from_line 2, to_line 2, replaced_chars 5, replaced 'first'}` | f6157952… |
| test_a_successful_write::test_the_privacy_r2_corruption_shape | `apply(... 1:17..1:40 "NEW")` → applied, echo shows the 23 chars | `apply_corroborated(...)`; also asserts the preview shows `'if action in _covered_a'` before anything is written | echo `'if action in _covered_a'`, 23 chars; whole-line part unchanged | b2c35f94… |

Two of those five in the 73 (`test_a_digest_the_read_issued_applies`,
`test_reference_and_v4a_produce_the_same_bytes`) were not in the ruling's list of 3 + 3.
They name whole-line columns (`0..len`), and the ruling covers every explicit column, so
they are rewritten the same way. The run that turned them red (the old test files against
the candidate source) is `old_tests_on_candidate.txt`: 9 failed. That is these 8 plus the
first version of my own narrowed-sub-range test. That log's header predates the
`citations.py` hash line; its `strict_patch.py` hash `4765a32f…` is the committed one.

The old uncorroborated call is refused with the new error:
`test_the_old_uncorroborated_column_call_is_refused_with_the_new_error` (2 paths) and
`test_host_run_eac7cacb_exact_call_is_refused_not_written`.

## Whole suite

| tree | result | bare RC | log |
|---|---|---|---|
| base 101da5c | 1209 passed, 3 skipped | PYTEST_RC=0, DOCKER_RC=0 | `suite_base.txt` |
| candidate | 1230 passed, 3 skipped | PYTEST_RC=0, DOCKER_RC=0 | `suite_candidate.txt` |

1230 = 1209 + 21 new tests. No red was added, and the 8 rewritten tests are green. The
card's figure of 1183 is older than this base; 1209 is measured. Import proof in each log:
`IMPORT_PROOF /home/linxuhao/stepflow-coords-r1-base/src/skillflow/__init__.py` and
`IMPORT_PROOF /home/linxuhao/stepflow-coords-r1/src/skillflow/__init__.py`. Each log also
prints the sha256 of the four changed sources as the container saw them. The candidate runs
were taken on the working tree just before the commit; the committed hashes are checked
against them in the final message. Runner: `run_in_throwaway.sh` (`docker run --rm --init
-m 3g`, the worktree's `src/` first on `PYTHONPATH`, pytest's exit code captured bare).

## Mutations (throwaway copies, never the worktree)

| mutation | red | ignitions | log |
|---|---|---|---|
| 3: corroboration check deleted | 16 failed / 107, RC=1 | 16 | `mutation3_corroboration_deleted.txt` |
| 1: whole-window default truncated to its first line | 2 failed / 107, RC=1 | 7 | `mutation1_window_truncated.txt` |
| 2: base `strict_patch.py` under the candidate's tests | 27 failed / 107, RC=1 | — | `mutation2_base_strict_patch.txt` |

Coverage of mutation 3: 16 firings across the 16 tests that send a column × 1 mutant; every one of them turned red.
Coverage of mutation 1: 7 firings across the 6 whole-window tests × 1 mutant. The other
whole-window tests cite one-line windows, where truncating to the first line changes
nothing.

## Out of scope — AItelier `templates/coding_impl.md`

`~/AItelier/templates/coding_impl.md` lines 47-53 still teach `(L,0)..(M,len(line M))`.
When the pin is bumped, replace exactly this text:

    To edit an existing file, cite it instead of copying it. Every `read` returns a
    `citation` for the window it served; pass its `sha` with the range you are
    replacing and only the new text — `{"file", "sha", "from_line", "from_col",
    "to_line", "to_col", "new_text"}` — in `references`. Lines are 1-based inside
    that window, columns 0-based, `to_col` exclusive; `(L,0)..(M,len(line M))`
    replaces whole lines. Any order: one snapshot, engine-applied, non-overlapping.
    Changing one word cites one line.

with:

    To edit an existing file, cite it instead of copying it. Every `read` returns a
    `citation` for the window it served. Send its `sha` and only the new text in
    `references`, with no more numbers than your intent needs:
    - `{"file", "sha", "new_text"}` replaces exactly the window that read served —
      read just the lines you mean to replace, then cite them;
    - add `"from_line"`/`"to_line"` (the numbers printed in the read's margin) to
      replace whole lines inside a wider window;
    - add `"from_col"`/`"to_col"` only to cut INSIDE a line. That takes two calls:
      the first writes nothing and returns `spans`, showing the exact text your
      columns cover and what they leave on the line, with a span `sha`. If that
      text is what you meant, resend the same reference with the span `sha`;
      otherwise fix the columns or drop them. Never count columns for a whole
      line — omit them.
    Any order: one snapshot, engine-applied, non-overlapping.

## What this does not do — stated plainly

- **A caller that resends the span without reading it still gets what its count covered.**
  `replay_candidate.txt` A1' resends eac7cacb's call citing the span it was shown, and
  writes `= 450`. The guarantee is that this write comes only after a call that wrote
  nothing and showed `DEFAULT_TIMEOUT_SECONDS = 3` with `0` kept. It does not tell a caller
  what to do with what it is shown.
- **Column edits cost two calls now**, including the 8 rewritten tests' shapes. Whole-line
  and whole-window edits stay at one call.
- **Explicit line numbers are not corroborated.** A lines-only range (`from_line`/`to_line`,
  no columns) is still applied in one call. I read the ruling's "every explicit
  line/column range" as the column case, because its mechanism and error are both stated
  for columns. Line numbers are copied from the read's printed margin, not counted from
  text. Requiring a span per line range would also end the frozen-frame property: a
  second edit with no reread in between
  (`test_a_second_edit_below_the_first_needs_no_read_in_between`). If lines are meant too,
  that is a further change.
- **The whole-window form moves the choice of range into the read's arguments**, which are
  0-based. In eac7cacb's own trace (ids 36/37) the agent asked `read(1679,1680)` meaning
  line 1679 and was served line 1680. The served line is in the result with its prefix.
- `~/AItelier/templates/coding_impl.md` is unchanged (above), by instruction.
- Not done, by instruction: no tag, no version bump, no build, no PyPI upload, no AItelier
  pin change, no State DAG write.

## Read-back

`worddiff_readback.txt` is `git diff --word-diff d8c490a` of every changed or added non-log
text file in the amendment: the three sources, the four test files, the two scripts, the
runner and this note. It was taken before the commit. The first commit's read-back of
`read_tools.py` and the fixture is in `d8c490a`. The edited regions were read back for
splice damage (duplicated adjacent lines, fused lines, cut sentences, stale tails); none
found.
