#!/bin/sh
# Stage 2: whole suites, the 73 reference tests, the eac7cacb replay and the
# byte identity of the rewritten tests, on base 101da5c and on the candidate.
# At most 3 containers at a time (another session's gate may hold a fourth).
E=/home/linxuhao/stepflow-coords-r1/evidence/coords-r4; S=/home/linxuhao/coords-r4-scratch/ev
T=/home/linxuhao/stepflow-coords-r1; B=/home/linxuhao/stepflow-coords-r1-base
REF="tests/test_reference_hunks.py tests/test_strict_patch_parser.py tests/test_output_targets.py"
echo "BASE_HEAD $(git -C $B rev-parse HEAD) CAND_HEAD $(git -C $T rev-parse HEAD)"
$E/run_in_throwaway.sh $T coords-r4-suite-cand $S/suite_candidate.txt & a=$!
$E/run_in_throwaway.sh $B coords-r4-suite-base $S/suite_base.txt & b=$!
$E/run_in_throwaway.sh $T coords-r4-ref-cand $S/reference73_candidate.txt $REF & c=$!
wait $a; echo SUITE_CAND_RC=$?; wait $b; echo SUITE_BASE_RC=$?; wait $c; echo REF_CAND_RC=$?
$E/run_in_throwaway.sh $B coords-r4-ref-base $S/reference73_base.txt $REF & a=$!
$E/replay.sh $T coords-r4-replay-cand $S/replay_candidate.txt & b=$!
$E/replay.sh $B coords-r4-replay-base $S/replay_base.txt & c=$!
wait $a; echo REF_BASE_RC=$?; wait $b; echo REPLAY_CAND_RC=$?; wait $c; echo REPLAY_BASE_RC=$?
$E/run_bytes.sh $T coords-r4-bytes-cand $S/rewritten_real_candidate.txt $S/rewritten_real_candidate.json & a=$!
$E/run_bytes.sh $B coords-r4-bytes-base $S/rewritten_real_base.txt $S/rewritten_real_base.json & b=$!
wait $a; echo BYTES_CAND_RC=$?; wait $b; echo BYTES_BASE_RC=$?
python3 $E/compare_bytes.py $S/rewritten_real_base.json $S/rewritten_real_candidate.json > $S/rewritten_real_compare.txt
echo COMPARE_RC=$?
