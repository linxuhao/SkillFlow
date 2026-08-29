"""A repo tool handed no project_root must refuse, not fall back to the CWD.

`Path("").resolve()` is the process working directory. For a hosted engine that
is the server's own checkout — verified as a git repository at `/app` in
AItelier's container. Three tools resolve `project_root` straight from their
argument with no guard of their own:

    tools/repo_apply/impl.py     dst = Path(project_root).resolve()
    tools/git_sync_pre/impl.py   root = Path(project_root).resolve()
    tools/repo_validate/impl.py  proj = Path(project_root)   # Path("") == '.'

repo_apply then copies files into `dst` and runs `git add -A` + `git commit`
there. The engine no longer hands them an empty root (see
test_repoless_run_has_no_repo_layer), and this is the second line: a host, a
generated pipeline or a hand-written hook can also under-specify the argument,
and the tool is the last place that can still say no.

Each test runs with the CWD moved into a tmp directory and asserts the tool
neither wrote to it nor reported on it.
"""

import subprocess

import pytest

from skillflow.tools.git_sync_pre.impl import git_sync_pre
from skillflow.tools.repo_apply.impl import repo_apply
from skillflow.tools.repo_validate.impl import repo_validate


@pytest.fixture
def cwd_repo(tmp_path, monkeypatch):
    """A git repo, made the process CWD — i.e. the thing that must NOT be hit."""
    d = tmp_path / "server_checkout"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / "UNRELATED.md").write_text("someone else's file", encoding="utf-8")
    monkeypatch.chdir(d)
    return d


def test_repo_apply_refuses_an_empty_project_root(cwd_repo, tmp_path):
    src = tmp_path / "step_out"
    src.mkdir()
    (src / "generated.py").write_text("print('hi')", encoding="utf-8")

    result = repo_apply(str(src), workspace_root=str(tmp_path), project_root="")

    assert result.get("applied") is False
    assert "absolute path" in result.get("error", ""), result
    assert not (cwd_repo / "generated.py").exists(), \
        "step output was copied into the process CWD"
    # …and nothing was committed on top of it.
    log = subprocess.run(["git", "log", "--oneline"], cwd=cwd_repo,
                         capture_output=True, text=True)
    assert log.stdout.strip() == ""


def test_repo_apply_refuses_a_relative_project_root(cwd_repo, tmp_path):
    src = tmp_path / "step_out"
    src.mkdir()
    (src / "generated.py").write_text("print('hi')", encoding="utf-8")

    result = repo_apply(str(src), workspace_root=str(tmp_path),
                        project_root="some/relative/dir")

    assert result.get("applied") is False
    assert "absolute path" in result.get("error", ""), result


def test_repo_apply_still_applies_to_a_real_absolute_root(tmp_path):
    """The control: the guard must not have broken the tool it protects."""
    src = tmp_path / "step_out"
    src.mkdir()
    (src / "generated.py").write_text("print('hi')", encoding="utf-8")
    dst = tmp_path / "real_repo"
    dst.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=dst, check=True)

    result = repo_apply(str(src), workspace_root=str(tmp_path),
                        project_root=str(dst))

    assert result.get("applied") is True, result
    assert (dst / "generated.py").exists()


def test_git_sync_pre_refuses_an_empty_project_root(cwd_repo):
    result = git_sync_pre("")

    assert result.get("synced") is False
    assert "absolute path" in result.get("error", ""), result
    # The CWD *is* a git repo with no remote, so the unguarded version answered
    # {"synced": True, "action": "skip"} about a repo it was never given.
    assert result.get("action") != "skip"


def test_repo_validate_refuses_an_empty_project_root(cwd_repo):
    (cwd_repo / "somefile.md").write_text("x", encoding="utf-8")

    result = repo_validate([{"tool": "file_exists", "files": ["*.md"]}],
                           project_root="")

    assert result.get("all_passed") is False
    assert "absolute path" in result.get("error", ""), result
    assert result.get("results") == [], \
        "it reported findings about the process CWD"


def test_pytest_refuses_an_empty_workspace_root(cwd_repo):
    """`pytest` resolves `(Path(workspace_root) / file)` — with "" that is the
    process CWD, so a relative test path names the SERVER's own test file and
    the tool runs and reports on it.

    Reachable on a repo-less run: it has no workspace_root to give (the engine
    omits the argument), and the `tool_creation` capability grants this tool to
    exactly such runs (AItelier's pipeline_forge `t_tool_impl`).
    """
    from skillflow.tools.pytest.impl import pytest as pytest_tool

    (cwd_repo / "test_theirs.py").write_text(
        "def test_theirs():\n    assert True\n", encoding="utf-8")

    result = pytest_tool("test_theirs.py", workspace_root="")

    assert result.get("verdict") == "failed"
    assert "absolute path" in result.get("feedback", ""), result


def test_pytest_still_runs_a_file_under_a_real_workspace_root(tmp_path):
    """The control: the guard must not have broken the tool it protects."""
    from skillflow.tools.pytest.impl import pytest as pytest_tool

    (tmp_path / "test_ours.py").write_text(
        "def test_ours():\n    assert True\n", encoding="utf-8")

    result = pytest_tool("test_ours.py", workspace_root=str(tmp_path))

    assert result.get("verdict") == "passed", result
