#!/bin/sh
# usage: run_in_throwaway.sh <tree> <container-name> <log> [pytest args...]
# Runs pytest in a throwaway --init container with <tree>/src first on the
# import path, and logs the import proof, the tree state and the hashes of the
# files this round changes. The exit code is pytest's, captured bare.
TREE=$1; NAME=$2; LOG=$3; shift 3
docker run --rm --init -m 3g --name "$NAME" -u 1000:1000 -e HOME=/tmp \
  -e PYTHONPATH="$TREE/src" -e PYTHONDONTWRITEBYTECODE=1 \
  -v /home/linxuhao/stepflow:/home/linxuhao/stepflow:ro \
  -v "$TREE:$TREE" -w "$TREE" aitelier:latest \
  sh -c 'echo "TREE_HEAD=$(git rev-parse HEAD 2>/dev/null) DIRTY_PATHS=$(git status --porcelain 2>/dev/null | wc -l)"; sha256sum src/skillflow/strict_patch.py src/skillflow/read_tools.py src/skillflow/tools/apply_patch/tool.yaml 2>&1; python -c "import skillflow; print(\"IMPORT_PROOF\", skillflow.__file__)"; python -c "import pytest, pytest_asyncio; print(\"PYTEST\", pytest.__version__, \"ASYNCIO\", pytest_asyncio.__version__)"; python -m pytest -p no:cacheprovider -q -rfE "$@"; rc=$?; echo "PYTEST_RC=$rc"; exit $rc' sh "$@" > "$LOG" 2>&1
rc=$?
echo "DOCKER_RC=$rc" >> "$LOG"
exit $rc
