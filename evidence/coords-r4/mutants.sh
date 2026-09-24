#!/bin/sh
# usage: mutants.sh <name>...   (control = no mutation; m3 = r1 review mutate.py verbatim)
# Each mutant runs the whole suite on a throwaway copy (tar, no .git) of the
# candidate tree. Logs go to $S; the exit codes are captured bare.
E=/home/linxuhao/stepflow-coords-r1/evidence/coords-r4
S=/home/linxuhao/coords-r4-scratch/ev; T=/home/linxuhao/stepflow-coords-r1
R=/home/linxuhao/.AItelier/director/reports/coords-r1-review-20260924
for m in "$@"; do
  C=/home/linxuhao/coords-r4-scratch/mut_$m; rm -rf "$C"; mkdir -p "$C"
  (cd $T && tar --exclude=./.git -cf - .) | (cd "$C" && tar -xf -)
  case $m in
    control) echo "MUTATED control (none)";;
    m3) python3 $R/mutate.py "$C" m3;;
    *) python3 $E/mutate_r4.py "$C" $m;;
  esac
  mrc=$?; echo "MUTATE_${m}_RC=$mrc"
  [ $mrc -ne 0 ] && continue
  for f in src/skillflow/strict_patch.py src/skillflow/citations.py; do diff -u "$T/$f" "$C/$f"; done > $S/mutant_${m}.diff.txt
  $E/run_in_throwaway.sh "$C" coords-r4-mut-$m $S/mutant_${m}_suite.txt
  echo "SUITE_${m}_RC=$?"
  n=0; [ -f "$C/IGNITIONS.log" ] && n=$(grep -c IGNITION "$C/IGNITIONS.log")
  echo "IGNITIONS_${m}=$n"
  [ -f "$C/IGNITIONS.log" ] && cp "$C/IGNITIONS.log" $S/mutant_${m}_ignitions.txt
  grep -E "^FAILED" $S/mutant_${m}_suite.txt | sed "s/ - .*//"
  tail -3 $S/mutant_${m}_suite.txt | head -1
done
