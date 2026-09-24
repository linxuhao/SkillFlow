# editing.the-coordinates-must-come-from-the-read-too (rev 2) — delivery notes

Attempt `attempt-684a5e14cc2e4a638de437aff4b24961`, branch `director/coords-r1-20260924`,
base `101da5c44e23aadbb0dcf3a423a0ac43286453bc` (release 1.5.79). Written 2026-09-24 (UTC).
Evidence convention: this directory (`evidence/coords-r1/`); the repo had no delivery-note
convention, only `evidence/artifact-revision-20260912/` wheel dumps. Every log is `.txt`.

## What changed

1. `src/skillflow/strict_patch.py` — a reference may omit `from_line`/`to_line`. With both
   omitted, `{file, sha, new_text}` replaces exactly the window the citation was issued for;
   every coordinate comes from the citation's record (start_line..end_line, column 0 to the
   end of the last line), on both the translated (journal) path and the strict path. Half a
   range is refused: one line without the other, or columns without lines. The echo
   (`replaced`) reports the resolved lines. Explicit lines and explicit columns are unchanged.
2. `src/skillflow/tools/apply_patch/tool.yaml` — the reference-mode contract now leads with
   the forms that carry no counted number (sha only; lines from the read's margin), puts
   columns under "cut INSIDE a line", and says a column is honoured as written (one short
   keeps the last character).
3. `src/skillflow/read_tools.py` — the read description says the sha with only `new_text`
   replaces exactly that window.
4. `tests/test_the_coordinates_come_from_the_read.py` (14 tests) and
   `tests/fixtures/eac7cacb_app_settings.py.txt` (the exact file host run eac7cacb edited).

## Route chosen, and the discriminating reason (criterion 1 asks for this)

Route: **the whole-window write** — the range written is the range read.

Discriminating reason. The digest route needs the caller to attach a digest of the text it
believes it is replacing. A caller cannot compute one: the citation digest is keyed with a
process-private secret, and a model cannot hash text by hand, so any digest it attaches
must have been issued by the engine. The engine issues digests only for windows a read
served. So a caller that holds the digest of exactly the text it means to replace already
holds a citation of that text, and can cite it with no number at all — the digest route,
taken to its end, is the whole-window route. For a sub-line span there is no digest the
caller can hold without a preview round trip in which the engine shows it the span first.

Why a miscounted column can no longer write silently in the new writing: the new writing
has no column (and, in its sha-only form, no line number either). The caller in eac7cacb
believed line 1679 was 27 characters long; in `{file, sha, new_text}` that belief has no
field to travel in. The text replaced is the text the read showed, and the engine re-checks
it by sha before writing (refused if it changed: `test_a_whole_window_whose_text_changed_is_refused_not_overwritten`).
This is not a citation that carries more lengths so the caller can count better (the
forbidden non-fix (a)); it removes the count. It is not a retype either (forbidden (b)):
the reference still has no field that could hold the original
(`test_a_reference_hunk_has_nowhere_to_put_the_original` stays green, and parse still
refuses `old_text`/`context`/`old_str`).

## a-mis-specified-range-cannot-write-silently

Pole 1 — the len-1 mistake reproduced, new writing: `test_the_len_minus_one_belief_has_no_field_to_travel_in`
(reference asserted to contain none of from_line/to_line/from_col/to_col; line 3 written as
`DEFAULT_TIMEOUT_SECONDS = 45`, asserted `!= ... = 450`). Log: `new_tests_candidate.txt`
(14 passed, PYTEST_RC=0, DOCKER_RC=0). Actual product, from `replay_candidate.txt` part C:

    sha + new_text only (read line 3)   applied=True   line 3 = 'DEFAULT_TIMEOUT_SECONDS = 45'
    product bytes b'import os\n\nDEFAULT_TIMEOUT_SECONDS = 45\n\nDEBUG = False\n'

Pole 2 — len+1 still loud, word for word: `test_the_len_plus_one_error_is_still_loud_and_word_for_word`.
Actual error, `replay_candidate.txt` part C (director's probe shape, strict path):

    to_col = len+1 (0..29)   applied=False   line 3 = 'DEFAULT_TIMEOUT_SECONDS = 30'
    error: apply_patch preflight: app/settings.py: to column 29 is past the end of line 3 (28 characters); reread the range

Through a real read (translated path) the same test also pins
`to column 29 is past the end of line 3 as the read served it (28 characters)`.

Without the change: the same 14 tests run against base `strict_patch.py`
(`mutation2_base_strict_patch.txt`): 11 failed, 89 passed, PYTEST_RC=1 — including pole 1
(`missing field(s) ['from_line', 'to_line']` on base, also visible in `replay_base.txt`).

## prove-it-on-the-edit-that-actually-went-wrong

Source: host run eac7cacb, `/tmp/refmode-3r_9rvh9/sf.db` trace ids 16/17 (read) and 22/23
(apply_patch), copied verbatim to `eac7cacb_trace_16_17_22_23.txt`. The file is
`/tmp/refmode-3r_9rvh9/repo` HEAD `1674e5f` `app/settings.py`, git blob
`a1f2b73a72018708afc0230a05f177d478802158`, sha256
`036440eb37d4428cd72f43650cdb1750abdddd86f67b026610afa941fdac0ffe`, 49,447 bytes; the
committed fixture has the same blob id and sha256 (the test asserts the sha256).

The replay re-issues the same read (`start_line=1674, end_line=1685`) and gets the window
the agent got — `start_line 1675, end_line 1684, end_col 19, start_byte 49155, end_byte 49447`,
equal to trace id 17 — then writes the same line with the same intent
(`replay_candidate.txt`, SCRIPT_RC=0, DOCKER_RC=0):

    A2 same read, same line, no columns   applied=True   line 1679 = 'DEFAULT_TIMEOUT_SECONDS = 45'
    A2 rest of file byte-identical: True
    A3 sha + new_text only                applied=True   line 1679 = 'DEFAULT_TIMEOUT_SECONDS = 45'

Tests: `test_host_run_eac7cacb_replayed_writes_the_line_it_meant`,
`test_host_run_eac7cacb_replayed_with_the_sha_alone` (the plan's own verification: the file
is otherwise byte-identical).

Second real case (not a substitute): wuxia run b3983d63, trace ids 2340/2341 (read
`start_line=70,end_line=84`) and 2354/2355 (apply_patch `to_col=58` on a 61-character
line), commit `5ac8714e` holds the result (blob `0eae2c14ca33fac6a762779d24ecf17278ea68db`,
copied to `wuxia_5ac8714e_unit_test_runner.gd.txt`). The replay reconstructs line 78 as it
was before id 2354, re-issues the read and gets the same content, range and bytes as trace
id 2341 (`True`), then:

    B1 trace id 2354 verbatim (to_col=58)  line 78 = '\t"res://tests/test_martial_arts_surface_census.gd",d",'
    B1 file == commit 5ac8714e bytes: True
    B2 same read, same line, no columns    line 78 = '\t"res://tests/test_martial_arts_surface_census.gd",'
    B3 sha + new_text only                 line 78 = '\t"res://tests/test_martial_arts_surface_census.gd",'

B1 ran on 1.5.79, whose result already echoed `replaced` (trace id 2358 shows the
58-character echo ending in `".g`), and the round's next call was `focused_check`.
An echo returned after the write did not stop it.

The 73 delivered reference-mode tests (`test_reference_hunks` + `test_strict_patch_parser`
+ `test_output_targets`), unchanged: candidate 73 passed, PYTEST_RC=0, DOCKER_RC=0
(`reference73_candidate.txt`); base 73 passed, PYTEST_RC=0 (`reference73_base.txt`).

## replace-what-was-read-without-naming-a-column

Pole 1 — `test_citing_the_sha_alone_replaces_the_line_that_was_read`: the director's
28-character line, read alone, cited as `{file, sha, new_text}` (asserted to be exactly those
three keys). Product bytes asserted equal to
`b'import os\n\nDEFAULT_TIMEOUT_SECONDS = 45\n\nDEBUG = False\n'`; the echo says it replaced
all 28 characters `DEFAULT_TIMEOUT_SECONDS = 30`. Same bytes printed in `replay_candidate.txt`.

Pole 2 — `test_a_narrowed_sub_range_is_still_honoured_as_written`: whole lines inside a
wider window (`from_line=3,to_line=3`) and a column cut (`3:26..3:28` -> `45`, echo `30`)
each produce the same bytes. The director's own `to_col=27` sub-range still does what it
says (`replay_candidate.txt` part C, `to_col = len-1 (0..27)` -> `= 450`, echo
`DEFAULT_TIMEOUT_SECONDS = 3`).

Also covered: a multi-line window replaces exactly the window
(`test_the_whole_window_is_exactly_the_window_and_nothing_either_side`); a whole-window
citation still lands after this run edits elsewhere in the file (journal translation);
half a range is refused, 4 shapes; an unissued sha is still refused.

## Whole suite

| tree | result | bare RC | log |
|---|---|---|---|
| base 101da5c | 1209 passed, 3 skipped | PYTEST_RC=0, DOCKER_RC=0 | `suite_base.txt` |
| candidate | 1223 passed, 3 skipped | PYTEST_RC=0, DOCKER_RC=0 | `suite_candidate.txt` |

1223 = 1209 + the 14 new tests; no red added. The card's figure of 1183 is older than this
base; 1209 is measured here. Import proof in each log: base
`IMPORT_PROOF /home/linxuhao/stepflow-coords-r1-base/src/skillflow/__init__.py`, candidate
`IMPORT_PROOF /home/linxuhao/stepflow-coords-r1/src/skillflow/__init__.py`. Each log also
prints the sha256 of the three changed source files as the container saw them (candidate
`strict_patch.py` da8027a3…, base 5340112d…); the candidate runs were taken on the
working tree before the commit, so compare those hashes with the committed files.
Runner: `run_in_throwaway.sh` (`docker run --rm --init -m 3g`, worktree `src/` first on
`PYTHONPATH`, pytest's exit code captured bare, never piped).

## Mutations (throwaway copies, not the worktree)

1. `mutation1_window_truncated.txt` — the whole-window default truncated to the window's
   first line (`mutation1_patch.py.txt`). IGNITIONS=7. Red: 2 —
   `test_the_whole_window_is_exactly_the_window_and_nothing_either_side`,
   `test_a_whole_window_whose_text_changed_is_refused_not_overwritten`. The other
   whole-window tests cite one-line windows, where the truncation is the identity; that is
   the coverage of this mutation: 7 firings across 6 whole-window tests x 1 mutant.
2. `mutation2_base_strict_patch.txt` — base `strict_patch.py` under the candidate's tests.
   Red: 11 of the 14 new tests (listed in the log); the 3 that stay green are the ones whose
   forms already existed on base (lines-only replay, narrowed sub-range, len+1).

## What this does not do — stated plainly

- **An explicit column is still executed as written, including a miscounted one.** The
  eac7cacb call verbatim (`to_col=27`) still writes `DEFAULT_TIMEOUT_SECONDS = 450` on the
  candidate, exactly as on base (`replay_candidate.txt` A1, `replay_base.txt` A1), and the
  wuxia call verbatim still writes the `d",` tail (B1). The guarantee above holds for the
  new writing only. Closing the explicit-column channel needs redundancy on every explicit
  cut, and the only redundancy a caller can supply without retyping is an engine-issued
  digest obtained through a preview round trip. Making that mandatory conflicts with three
  of the 73 delivered tests this card requires green —
  `test_one_word_cites_one_line_and_leaves_the_rest_alone`,
  `test_unordered_references_match_descending_ones_byte_for_byte`,
  `test_reference_mode_runs_end_to_end_through_the_engine` — and, elsewhere in the suite,
  `test_the_result_says_what_it_replaced`,
  `test_citing_inside_a_span_this_run_replaced_is_refused_by_name` and
  `test_the_privacy_r2_corruption_shape`: each sends a sub-line cut with no corroboration in
  one call and asserts `applied` is true. That list is read from the test sources; it was
  not run as a mutant.
  A token-boundary rule would refuse all three recorded incidents (27 splits `30`, 58 splits
  `gd`, privacy r2's 40 splits `_covered_actions`), but it judges whether a column looks
  right, which the card rules out, and `to_col=26` would still write `= 4530`. This
  conflict is the director's to rule on.
- **The host prompt still teaches columns.** `~/AItelier/templates/coding_impl.md` (the
  coding_impl template that eac7cacb's prompt came from) says `(L,0)..(M,len(line M))`
  replaces whole lines. That file is in the AItelier repo, outside this round.
- **The whole-window form moves the choice of range into the read's arguments**, which are
  0-based. In eac7cacb's own trace (id 36/37) the agent asked `read(1679,1680)` meaning line
  1679 and was served line 1680. The served text and line prefix are in the result, but a
  caller that cites a mis-aimed read without looking at it replaces the line it was shown.
- The lines-only form (`from_line`/`to_line`, no columns) already existed on base 1.5.79;
  the replay's A2/B2 lines pass on base too. What is new is the sha-only form, the refusal
  of half ranges, and the contract text.
- Not done, by instruction: no tag, no version bump, no build, no PyPI upload, no AItelier
  pin change, no State DAG write.

## Read-back

`worddiff_readback.txt` is `git diff --word-diff` of every changed or added non-log text
file in this commit (the three sources, the new test, `replay_real_edits.py`,
`run_in_throwaway.sh`, this note), taken before the commit. The two copied data files are
identified by blob id instead of a word diff, because each is a byte copy of a file named
above: `tests/fixtures/eac7cacb_app_settings.py.txt` = `a1f2b73a…` and
`wuxia_5ac8714e_unit_test_runner.gd.txt` = `0eae2c14…`; `wuxia_b3983d63_trace2341_served.json`
and `eac7cacb_trace_16_17_22_23.txt` are extracts of the two trace databases, and
`mutation1_patch.py.txt` is the mutation script as run. The edited regions were read back
for splice damage (duplicated adjacent lines, fused lines, cut sentences, stale tails);
none found.
