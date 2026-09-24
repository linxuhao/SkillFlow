#!/bin/bash
# Stage 1: the rewritten test file on this tree and on the r3 candidate, and
# the reviews' probe scripts, unmodified, on this tree.
E=/home/linxuhao/stepflow-coords-r1/evidence/coords-r4; S=/home/linxuhao/coords-r4-scratch/ev
T=/home/linxuhao/stepflow-coords-r1; R3T=/home/linxuhao/coords-r4-scratch/r3tree
R3=/home/linxuhao/.AItelier/director/reports/coords-r3-review-20260924
N=tests/test_the_journal_records_what_the_applier_wrote.py
echo "CAND_HEAD $(git -C $T rev-parse HEAD) R3TREE_HEAD $(git -C $R3T rev-parse HEAD)"
$E/run_in_throwaway.sh $T coords-r4-new-cand $S/new_tests_candidate.txt $N & a=$!
$E/run_in_throwaway.sh $R3T coords-r4-new-r3 $S/new_tests_on_r3.txt $N & b=$!
$E/run_py.sh $T coords-r4-probe1 $S/probe_candidate.txt $R3/probe.py & c=$!
$E/run_py.sh $T coords-r4-probe2 $S/probe2_candidate.txt $R3/probe2.py & d=$!
wait $a; echo NEW_CAND_RC=$?; wait $b; echo NEW_R3_RC=$?
wait $c; echo PROBE_RC=$?; wait $d; echo PROBE2_RC=$?
$E/run_py.sh $T coords-r4-probe3 $S/probe3_candidate.txt $R3/probe3.py & a=$!
$E/run_py.sh $T coords-r4-probe4 $S/probe4_candidate.txt $R3/probe4.py & b=$!
$E/run_py.sh $T coords-r4-probe5 $S/probe5_candidate.txt $R3/probe5.py & c=$!
$E/run_py.sh $T coords-r4-probe6 $S/probe6_candidate.txt $R3/probe6.py & d=$!
wait $a; echo PROBE3_RC=$?; wait $b; echo PROBE4_RC=$?
wait $c; echo PROBE5_RC=$?; wait $d; echo PROBE6_RC=$?
$E/run_py.sh $T coords-r4-probe8 $S/probe8_candidate.txt $R3/probe8.py & a=$!
wait $a; echo PROBE8_RC=$?
# What moved since the r3 review ran the same scripts on the r3 candidate.
for p in probe probe2 probe3 probe4 probe5 probe6 probe8; do
  diff <(grep -E "CASE|VERDICT|actual|CORRECT|applied|line [0-9]+ =" $R3/${p}_candidate.txt) \
       <(grep -E "CASE|VERDICT|actual|CORRECT|applied|line [0-9]+ =" $S/${p}_candidate.txt) > $S/${p}_diff_vs_r3review.txt
  echo "${p}_DIFF_RC=$?"
done
