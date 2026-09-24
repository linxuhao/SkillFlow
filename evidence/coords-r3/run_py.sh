#!/bin/sh
# usage: run_py.sh <tree> <name> <log> <script> [args]
# The review probes, run unmodified, with both review directories mounted read-only.
TREE=$1; NAME=$2; LOG=$3; shift 3
R1=/home/linxuhao/.AItelier/director/reports/coords-r1-review-20260924
R2=/home/linxuhao/.AItelier/director/reports/coords-r2-review-20260924
docker run --rm --init -m 3g --name "$NAME" -u 1000:1000 -e HOME=/tmp \
  -e PYTHONPATH="$TREE/src" -e PYTHONDONTWRITEBYTECODE=1 -e REAL_FILE=$R1/eac7cacb_real_settings.py \
  -v /home/linxuhao/stepflow:/home/linxuhao/stepflow:ro \
  -v "$TREE:$TREE:ro" -v "$R1:$R1:ro" -v "$R2:$R2:ro" -w /tmp aitelier:latest \
  python "$@" > "$LOG" 2>&1
rc=$?
echo "DOCKER_RC=$rc" >> "$LOG"
exit $rc
