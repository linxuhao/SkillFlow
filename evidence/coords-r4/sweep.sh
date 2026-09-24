#!/bin/sh
# The sweep: the r3 review's fuzz7.py and fuzz8.py unmodified, and fuzz8r.py
# (own edits by V4A, then by references in both insertion forms), seeds 7 and
# 8, 400 trials each. Four containers at a time.
E=/home/linxuhao/stepflow-coords-r1/evidence/coords-r4; S=/home/linxuhao/coords-r4-scratch/ev
T=/home/linxuhao/stepflow-coords-r1
R3=/home/linxuhao/.AItelier/director/reports/coords-r3-review-20260924
for seed in 7 8; do
  FUZZ_SEED=$seed $E/run_py.sh $T coords-r4-fuzz7-$seed $S/fuzz7_seed$seed.txt $R3/fuzz7.py & a=$!
  FUZZ_SEED=$seed $E/run_py.sh $T coords-r4-fuzz8-$seed $S/fuzz8_seed$seed.txt $R3/fuzz8.py & b=$!
  OWN=v4a FUZZ_SEED=$seed $E/run_py.sh $T coords-r4-fuzz8rv-$seed $S/fuzz8r_v4a_seed$seed.txt $E/fuzz8r.py & c=$!
  OWN=ref FUZZ_SEED=$seed $E/run_py.sh $T coords-r4-fuzz8rr-$seed $S/fuzz8r_ref_seed$seed.txt $E/fuzz8r.py & d=$!
  wait $a; echo FUZZ7_SEED${seed}_RC=$?; wait $b; echo FUZZ8_SEED${seed}_RC=$?
  wait $c; echo FUZZ8R_V4A_SEED${seed}_RC=$?; wait $d; echo FUZZ8R_REF_SEED${seed}_RC=$?
done
# The refusals no table row returned (the journal was broken), listed by trial.
for seed in 7 8; do
  OWN=ref DUMP=untranslated FUZZ_SEED=$seed $E/run_py.sh $T coords-r4-dump-$seed $S/fuzz8r_ref_seed${seed}_untranslated.txt $E/fuzz8r.py & p=$!
  wait $p; echo DUMP_SEED${seed}_RC=$?
done
