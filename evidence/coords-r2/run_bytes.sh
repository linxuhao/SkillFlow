#!/bin/sh
# usage: run_bytes.sh <tree> <name> <log> <out.json>
# The review's run_bytes.sh, with the output JSON written to this scratch dir
# and the review directory mounted read-only.
TREE=$1; NAME=$2; LOG=$3; OUT=$4
R=/home/linxuhao/.AItelier/director/reports/coords-r1-review-20260924
S=/home/linxuhao/coords-r2-scratch
T=tests/test_reference_hunks.py
W=tests/test_a_successful_write_leaves_the_next_coordinates_valid.py
docker run --rm --init -m 3g --name "$NAME" -u 1000:1000 -e HOME=/tmp \
  -e PYTHONPATH="$TREE/src" -e PYTHONDONTWRITEBYTECODE=1 \
  -v /home/linxuhao/stepflow:/home/linxuhao/stepflow:ro \
  -v "$TREE:$TREE:ro" -v "$R:$R:ro" -v "$S:$S" -w "$TREE" aitelier:latest \
  sh -c "python -c \"import skillflow; print(\\\"IMPORT_PROOF\\\", skillflow.__file__)\"; python -m pytest -p no:cacheprovider -q --basetemp=/tmp/bt $T::test_a_digest_the_read_issued_applies $T::test_reference_and_v4a_produce_the_same_bytes $T::test_one_word_cites_one_line_and_leaves_the_rest_alone $T::test_unordered_references_match_descending_ones_byte_for_byte tests/test_output_targets.py::test_reference_mode_runs_end_to_end_through_the_engine $W::test_citing_inside_a_span_this_run_replaced_is_refused_by_name $W::test_the_result_says_what_it_replaced $W::test_the_privacy_r2_corruption_shape; rc=\$?; echo PYTEST_RC=\$rc; python $R/hash_tmp.py /tmp/bt $OUT; exit \$rc" > "$LOG" 2>&1
rc=$?
echo "DOCKER_RC=$rc" >> "$LOG"
exit $rc
