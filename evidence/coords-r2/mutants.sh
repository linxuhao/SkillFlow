#!/bin/sh
# usage: mutants.sh <name>...   (control = no mutation; m3 = review mutate.py verbatim)
S=/home/linxuhao/coords-r2-scratch; T=/home/linxuhao/stepflow-coords-r1
R=/home/linxuhao/.AItelier/director/reports/coords-r1-review-20260924
for m in "$@"; do
  C=$S/mut_$m; rm -rf "$C"; mkdir -p "$C"
  (cd $T && tar --exclude=./.git -cf - .) | (cd "$C" && tar -xf -)
  case $m in
    control) echo "MUTATED control (none)";;
    m3) python3 $R/mutate.py "$C" m3;;
    m3b_review) python3 $R/mutate.py "$C" m3b;;
    *) python3 $S/mutate_r2.py "$C" $m;;
  esac
  mrc=$?; echo "MUTATE_${m}_RC=$mrc"
  [ $mrc -ne 0 ] && continue
  for f in src/skillflow/strict_patch.py src/skillflow/citations.py; do diff -u "$T/$f" "$C/$f"; done > $S/mutant_${m}.diff.txt
  $S/suite.sh "$C" coords-r2-mut-$m $S/mutant_${m}_suite.txt
  echo "SUITE_${m}_RC=$?"
  n=0; [ -f "$C/IGNITIONS.log" ] && n=$(grep -c IGNITION "$C/IGNITIONS.log")
  echo "IGNITIONS_${m}=$n"
  grep -E "^FAILED" $S/mutant_${m}_suite.txt | sed "s/ - .*//"
  tail -3 $S/mutant_${m}_suite.txt | head -1
done
