#!/bin/sh
# usage: run_py.sh <tree> <name> <log> <script> [args]   (review probes, unmodified, read-only mount)
TREE=$1; NAME=$2; LOG=$3; shift 3
R=/home/linxuhao/.AItelier/director/reports/coords-r1-review-20260924
docker run --rm --init -m 3g --name "$NAME" -u 1000:1000 -e HOME=/tmp \
  -e PYTHONPATH="$TREE/src" -e PYTHONDONTWRITEBYTECODE=1 -e REAL_FILE=$R/eac7cacb_real_settings.py \
  -v /home/linxuhao/stepflow:/home/linxuhao/stepflow:ro \
  -v "$TREE:$TREE" -v "$R:$R:ro" -w "$TREE" aitelier:latest \
  python "$@" > "$LOG" 2>&1
rc=$?
echo "DOCKER_RC=$rc" >> "$LOG"
exit $rc
