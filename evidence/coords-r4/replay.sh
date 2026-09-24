#!/bin/sh
# usage: replay.sh <tree-under-test> <name> <log>   (r1 replay script, unmodified)
TREE=$1; NAME=$2; LOG=$3; CAND=/home/linxuhao/stepflow-coords-r1
docker run --rm --init -m 3g --name "$NAME" -u 1000:1000 -e HOME=/tmp \
  -e PYTHONPATH="$TREE/src" -e PYTHONDONTWRITEBYTECODE=1 \
  -v /home/linxuhao/stepflow:/home/linxuhao/stepflow:ro \
  -v "$TREE:$TREE:ro" -v "$CAND:$CAND:ro" -w /tmp aitelier:latest \
  sh -c "python $CAND/evidence/coords-r1/replay_real_edits.py; rc=\$?; echo SCRIPT_RC=\$rc; exit \$rc" > "$LOG" 2>&1
rc=$?
echo "DOCKER_RC=$rc" >> "$LOG"
exit $rc
