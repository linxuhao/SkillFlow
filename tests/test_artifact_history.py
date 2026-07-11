"""Artifact history: git-versioned step outputs survive goal-loop overwrites.

_step_commit rmtree-wipes {step}/ before renaming the new staging in, so a
re-run of the same step used to destroy the prior output. With
artifact_history=True, each promoted step dir is committed to a git repo at the
workspace root, so every iteration stays recoverable for tracing.
"""

import subprocess
from pathlib import Path

from skillflow.core import SkillFlow


def _write_step(root: Path, config: str, step: str, content: str):
    d = root / config / step
    d.mkdir(parents=True, exist_ok=True)
    (d / "out.txt").write_text(content, encoding="utf-8")


def test_step_output_versions_preserved_across_overwrites(tmp_path):
    sf = SkillFlow(":memory:", workspace_base=str(tmp_path / "ws"),
                   artifact_history=True)
    root = sf._workspace.get_project_path("p")
    root.mkdir(parents=True, exist_ok=True)

    # Iteration 1: write + commit
    _write_step(root, "dpe_game", "3", "VERSION-ONE")
    sf._artifact_commit("p", "dpe_game", "3", "run-aaaa")
    # Iteration 2: OVERWRITE the same step dir + commit (simulates a goal-loop
    # re-run: _step_commit would rmtree the old dir first).
    _write_step(root, "dpe_game", "3", "VERSION-TWO")
    sf._artifact_commit("p", "dpe_game", "3", "run-aaaa")

    versions = sf.step_output_versions("p", "dpe_game", "3")
    assert len(versions) == 2, f"expected 2 versions, got {versions}"

    # The OLD output is recoverable from history (the whole point).
    oldest = versions[-1]["commit"]
    recovered = subprocess.run(
        ["git", "show", f"{oldest}:dpe_game/3/out.txt"],
        cwd=str(root), capture_output=True, text=True)
    assert recovered.stdout.strip() == "VERSION-ONE"
    # Working tree still has the latest.
    assert (root / "dpe_game" / "3" / "out.txt").read_text() == "VERSION-TWO"


def test_trace_db_and_staging_are_gitignored(tmp_path):
    sf = SkillFlow(":memory:", workspace_base=str(tmp_path / "ws"),
                   artifact_history=True)
    root = sf._workspace.get_project_path("p")
    root.mkdir(parents=True, exist_ok=True)
    # Volatile files that must NOT enter history.
    (root / "trace.db").write_text("x" * 1000, encoding="utf-8")
    (root / "trace.db-wal").write_text("y" * 1000, encoding="utf-8")
    (root / "dpe_game" / "3.tmp").mkdir(parents=True, exist_ok=True)
    (root / "dpe_game" / "3.tmp" / "staging.txt").write_text("stage", encoding="utf-8")
    _write_step(root, "dpe_game", "3", "real")
    sf._artifact_commit("p", "dpe_game", "3", "r")

    tracked = subprocess.run(["git", "ls-files"], cwd=str(root),
                             capture_output=True, text=True).stdout
    assert "dpe_game/3/out.txt" in tracked
    assert "trace.db" not in tracked
    assert ".tmp" not in tracked


def test_off_by_default_no_commits(tmp_path):
    sf = SkillFlow(":memory:", workspace_base=str(tmp_path / "ws"))  # no flag
    root = sf._workspace.get_project_path("p")
    root.mkdir(parents=True, exist_ok=True)
    _write_step(root, "dpe_game", "3", "x")
    # step_output_versions is empty and no git repo is created.
    assert sf.step_output_versions("p", "dpe_game", "3") == []
    assert not (root / ".git").exists()
