#!/bin/sh
# usage: run_py.sh <tree> <name> <log> <script> [args]
# A review script, run unmodified, with the r1, r2 and r3 review directories
# mounted read-only. OWN, FUZZ_SEED, FUZZ_TRIALS and DUMP pass through.
TREE=$1; NAME=$2; LOG=$3; shift 3
R1=/home/linxuhao/.AItelier/director/reports/coords-r1-review-20260924
R2=/home/linxuhao/.AItelier/director/reports/coords-r2-review-20260924
R3=/home/linxuhao/.AItelier/director/reports/coords-r3-review-20260924
docker run --rm --init -m 3g --name "$NAME" -u 1000:1000 -e HOME=/tmp \
  -e PYTHONPATH="$TREE/src" -e PYTHONDONTWRITEBYTECODE=1 \
  -e OWN=${OWN:-v4a} -e FUZZ_SEED=${FUZZ_SEED:-7} -e FUZZ_TRIALS=${FUZZ_TRIALS:-400} \
  -e DUMP=${DUMP:-} -e REAL_FILE=$R1/eac7cacb_real_settings.py \
  -v /home/linxuhao/stepflow:/home/linxuhao/stepflow:ro \
  -v "$TREE:$TREE:ro" -v "$R1:$R1:ro" -v "$R2:$R2:ro" -v "$R3:$R3:ro" -w /tmp aitelier:latest \
  sh -c "sha256sum $TREE/src/skillflow/citations.py $TREE/src/skillflow/strict_patch.py; python -c \"import skillflow; print(\\\"IMPORT_PROOF\\\", skillflow.__file__)\"; python \"\$@\"" sh "$@" > "$LOG" 2>&1
rc=$?
echo "DOCKER_RC=$rc" >> "$LOG"
exit $rc
