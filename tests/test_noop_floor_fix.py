"""Regression tests for the existing-repo "no-op floor" data-corruption bug.

Two skillflow-side guarantees the host (AItelier) relies on after the fix:

1. ``repo_apply`` on an EMPTY source dir is a no-op SUCCESS, not an error.
   A legitimate "no change needed" step promotes empty staging; on_deliver
   must not retry/fail it.

2. The generated directory read tools (``list_<label>`` / ``search_<label>``)
   exclude ``.git`` and build/dependency caches and cap their output, so a
   single ``list_repo_root`` can't dump the whole ``.git`` tree (tens of
   thousands of tokens) into the agent's context.
"""

import json
import subprocess
from pathlib import Path

from skillflow.read_tools import _is_blocked_path, make_read_tool_fns
from skillflow.tool_loader import ToolLoader

_REAL_TOOLS = Path(__file__).parent.parent / "src" / "skillflow" / "tools"


def test_repo_apply_empty_source_is_noop_success(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    src = tmp_path / "empty_staging"
    src.mkdir()  # no files

    repo_apply = ToolLoader(_REAL_TOOLS).load_fn("repo_apply")
    res = repo_apply(str(src), project_root=str(repo))

    assert res["applied"] is True
    assert res["files"] == []
    assert res.get("committed") is False
    assert "error" not in res


def test_is_blocked_path_excludes_vcs_and_build_dirs():
    assert _is_blocked_path(Path(".git/objects/ab/cd"))
    assert _is_blocked_path("node_modules/foo/bar.js")
    assert _is_blocked_path("pkg/__pycache__/x.pyc")
    assert not _is_blocked_path("src/main.py")
    assert not _is_blocked_path("docs/readme.md")


def test_list_repo_tool_excludes_git_tree(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git" / "objects").mkdir(parents=True)
    (repo / ".git" / "objects" / "deadbeef").write_text("binblob")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("x = 1\n")

    specs = [{"source_type": "repository", "mode": "tool"}]
    fns = make_read_tool_fns(specs, str(tmp_path), code_root=str(repo))

    list_fn = next(fn for name, fn in fns.items() if name.startswith("list_"))
    payload = json.loads(list_fn())
    names = [e["name"] for e in payload["files"]]

    assert "src/main.py" in names
    assert not any(".git" in n for n in names), names
