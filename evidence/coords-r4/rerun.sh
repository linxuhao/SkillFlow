#!/bin/sh
# After the M3 test was added: stage 2 and the mutants again, at most 3
# containers at a time. (Stage 1 was rerun first; its driver is stage1_driver.txt.)
E=/home/linxuhao/stepflow-coords-r1/evidence/coords-r4; S=/home/linxuhao/coords-r4-scratch/ev
sh $E/stage2.sh > $S/stage2_driver.txt 2>&1; echo STAGE2_RC=$?
sh $E/mutants.sh control point blankins > $S/mutants_run_1.txt 2>&1 & a=$!
sh $E/mutants.sh ends collapse m3 > $S/mutants_run_2.txt 2>&1 & b=$!
sh $E/mutants.sh m3b m5 m6 m7 > $S/mutants_run_3.txt 2>&1 & c=$!
wait $a; echo DRIVER1_RC=$?; wait $b; echo DRIVER2_RC=$?; wait $c; echo DRIVER3_RC=$?
echo ALL_DONE
