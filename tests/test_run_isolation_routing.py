"""Per-run isolation: every root a run resolves must be ITS root, not its
project's.

The hazard these tests pin down is not hypothetical arithmetic: two runs that
share one `repo_path` both reach `repo_apply`, which copies its step output into
the destination and then runs `git add -A` (tools/repo_apply/impl.py). Whichever
commits first stages *everything present in the tree at that moment*, including a
file the other run copied in a millisecond earlier, and commits it under its own
project/task name with a file count that is wrong. Nothing errors.

`test_shared_root_first_committer_absorbs_the_other` reproduces exactly that,
with the REAL repo_apply on both sides and a deterministic barrier — no threads,
no sleeps: the barrier fires inside run A's `git add`, which is the only window
in which the defect exists. It documents the unisolated behaviour and must keep
passing, because it is what proves the barrier reproduces the real interleaving
rather than a rearrangement of files nobody staged.

Every other test here asserts the isolated behaviour and fails before the
run-aware resolution lands.
"""

import subprocess
from pathlib import Path

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.exceptions import IsolationUnavailable
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager

_REAL_TOOLS = Path(__file__).parent.parent / "src" / "skillflow" / "tools"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True).stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)
    return _git(path, "rev-parse", "HEAD")


def _apply_graph(name: str) -> PipelineGraph:
    node = StepNode(
        id="s1", step_type="agent", output_mode="content",
        output_fixed={"out": "out.txt"},
        lifecycle={"on_deliver": {"tool": "repo_apply",
                                  "params": {"source_dir": "$STEP_DIR"}}},
        transitions=[Transition(to=None)],
    )
    return PipelineGraph(name=name, begin="s1", steps=[node])


def _sf(tmp_path, resolver, graph_name="apply"):
    sf = SkillFlow(":memory:")
    sf._tool_loader = ToolLoader(_REAL_TOOLS)
    sf._workspace = WorkspaceManager(
        str(tmp_path / "ws"), projects_base=str(tmp_path / "projects"),
        code_path_resolver=resolver)
    sf.register_graph(_apply_graph(graph_name))
    return sf


def _stage_and_start(sf, pid, graph_name, filename, content):
    """Create+start a run and write the agent's file into its staging dir.

    Returns (run_id, claim_token). The run is left UNCONFIRMED, which is the
    only state from which the delivery interleaving below can be driven.
    """
    rid = sf.create_run(graph_name, {"project_id": pid}, project_id=pid)
    sf.start_run(rid)
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)
    tmp = sf._workspace.get_step_tmp_dir(pid, graph_name, "s1")
    (tmp / filename).write_text(content)
    return rid, claimed.token


class _AddBarrier:
    """Fire `callback` once, inside repo_apply's `git add -A`.

    That is the real window: repo_apply has copied its own files into the
    destination and has not yet staged them. Patched into the tool module's own
    `subprocess` reference, so the REAL repo_apply runs on both sides.
    """

    def __init__(self, real_run, callback):
        self._real = real_run
        self._cb = callback
        self.fired = False

    def __call__(self, args, **kwargs):
        if (not self.fired and isinstance(args, list)
                and args[:3] == ["git", "add", "-A"]):
            self.fired = True
            self._cb()
        return self._real(args, **kwargs)


def test_shared_root_first_committer_absorbs_the_other(tmp_path, monkeypatch):
    """UNISOLATED behaviour, reproduced — the reason isolation exists.

    Two runs, one repo_path, both real repo_apply. Run B's copy lands between
    run A's copy and run A's `git add -A`; A's commit therefore contains B's
    file and says so nowhere.
    """
    src = tmp_path / "src"
    base = _init_repo(src)

    sf = _sf(tmp_path, lambda pid: str(src))
    rid_a, tok_a = _stage_and_start(sf, "pA", "apply", "a.txt", "A\n")
    rid_b, tok_b = _stage_and_start(sf, "pB", "apply", "b.txt", "B\n")

    import skillflow.tools.repo_apply.impl as apply_impl

    def _b_delivers():
        sf.confirm_step(tok_b, StepResult(outputs={}, flags={}))

    barrier = _AddBarrier(subprocess.run, _b_delivers)
    monkeypatch.setattr(apply_impl.subprocess, "run", barrier)
    sf.confirm_step(tok_a, StepResult(outputs={}, flags={}))

    assert barrier.fired, "barrier never fired — the interleaving was not real"
    first = _git(src, "log", "--format=%H %s", "--reverse").splitlines()[1]
    files = _git(src, "show", "--name-only", "--format=", first.split()[0]).split()
    assert set(files) == {"a.txt", "b.txt"}, (
        f"expected the first committer to absorb both files, got {files}")
    assert "(1 file(s))" in first, f"commit message understated the truth: {first}"


def test_two_isolated_runs_do_not_cross_stage(tmp_path, monkeypatch):
    """The positive contract: same source repo, two runs, disjoint commits.

    Same barrier, same real repo_apply. The only difference is that the host
    resolver is asked WHICH RUN is asking.
    """
    src = tmp_path / "src"
    base = _init_repo(src)
    wt = {}
    for name in ("A", "B"):
        path = tmp_path / f"wt{name}"
        subprocess.run(["git", "worktree", "add", "-q", "-b", f"codex/run/{name}",
                        str(path), base], cwd=src, check=True)
        wt[name] = path

    roots = {}

    def resolver(project_id, run_id=None):
        return str(roots.get(run_id, src))

    sf = _sf(tmp_path, resolver)
    rid_a, tok_a = _stage_and_start(sf, "pA", "apply", "a.txt", "A\n")
    roots[rid_a] = wt["A"]
    rid_b, tok_b = _stage_and_start(sf, "pB", "apply", "b.txt", "B\n")
    roots[rid_b] = wt["B"]

    import skillflow.tools.repo_apply.impl as apply_impl
    barrier = _AddBarrier(
        subprocess.run,
        lambda: sf.confirm_step(tok_b, StepResult(outputs={}, flags={})))
    monkeypatch.setattr(apply_impl.subprocess, "run", barrier)
    sf.confirm_step(tok_a, StepResult(outputs={}, flags={}))

    assert barrier.fired, "barrier never fired — the interleaving was not real"
    for name, fname in (("A", "a.txt"), ("B", "b.txt")):
        tip = _git(wt[name], "rev-parse", "HEAD")
        assert tip != base, f"run {name} committed nothing"
        touched = _git(wt[name], "show", "--name-only", "--format=", tip).split()
        assert touched == [fname], f"run {name} committed {touched}"
        assert not (wt[name] / ("b.txt" if name == "A" else "a.txt")).exists()
    assert _git(src, "rev-parse", "HEAD") == base, "source checkout moved"
    assert _git(src, "status", "--porcelain") == "", "source checkout dirtied"
    assert not (src / "a.txt").exists() and not (src / "b.txt").exists()


def test_project_root_token_resolves_the_run_root(tmp_path):
    """`$PROJECT_ROOT` expanded to projects_base/<project_id> WITHOUT asking
    the resolver, so a config using the token bypassed isolation entirely."""
    src = tmp_path / "src"
    _init_repo(src)
    ws = WorkspaceManager(str(tmp_path / "ws"),
                          projects_base=str(tmp_path / "projects"),
                          code_path_resolver=lambda pid, run_id=None:
                          str(src) if run_id == "R1" else None)
    out = ws.resolve_variables("p", "cfg", "s1", {"project_root": "$PROJECT_ROOT"},
                               run_id="R1")
    assert out["project_root"] == str(src.resolve())


def test_project_root_token_fails_closed_without_a_root(tmp_path):
    ws = WorkspaceManager(str(tmp_path / "ws"),
                          projects_base=str(tmp_path / "projects"),
                          code_path_resolver=lambda pid, run_id=None: False)
    with pytest.raises(IsolationUnavailable):
        ws.resolve_variables("p", "cfg", "s1", {"project_root": "$PROJECT_ROOT"},
                             run_id="R1")


def test_a_literal_project_root_cannot_outrank_declared_isolation(tmp_path):
    """A tool node that hardcodes project_root must not escape the run root."""
    src = tmp_path / "src"
    _init_repo(src)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    seen = {}

    def probe(project_root: str = "", **_):
        seen["project_root"] = project_root
        return {"passed": True}

    sf = SkillFlow(":memory:")
    sf._tool_loader = ToolLoader(_REAL_TOOLS)
    sf._tool_loader.register_dynamic_tool("probe", {}, probe)
    sf._workspace = WorkspaceManager(
        str(tmp_path / "ws"), projects_base=str(tmp_path / "projects"),
        code_path_resolver=lambda pid, run_id=None: str(src))
    node = StepNode(id="t1", step_type="tool", tool_name="probe",
                    tool_params={"project_root": str(elsewhere)},
                    transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name="lit", begin="t1", steps=[node]))
    rid = sf.create_run("lit", {"project_id": "p"}, project_id="p")
    sf.start_run(rid)
    sf.advance_run(rid)
    assert seen["project_root"] == str(src.resolve())


def test_isolation_failure_is_not_swallowed_by_best_effort_filling(tmp_path):
    """The tool-node path fills roots inside `except Exception: log and go on`.
    A fail-closed isolation error must not be absorbed by it."""
    def boom(project_id, run_id=None):
        raise IsolationUnavailable("worktree for run is gone")

    def probe(project_root: str = "", **_):
        return {"passed": True}

    sf = SkillFlow(":memory:")
    sf._tool_loader = ToolLoader(_REAL_TOOLS)
    sf._tool_loader.register_dynamic_tool("probe", {}, probe)
    sf._workspace = WorkspaceManager(
        str(tmp_path / "ws"), projects_base=str(tmp_path / "projects"),
        code_path_resolver=boom)
    node = StepNode(id="t1", step_type="tool", tool_name="probe",
                    transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name="boom", begin="t1", steps=[node]))
    rid = sf.create_run("boom", {"project_id": "p"}, project_id="p")
    sf.start_run(rid)
    with pytest.raises(IsolationUnavailable):
        sf.advance_run(rid)


def test_a_one_argument_resolver_still_works(tmp_path):
    """Required by the existing suite (tests/test_existing_repo_apply.py and
    others pass `lambda pid: ...`), so the run argument is optional, not new."""
    src = tmp_path / "src"
    _init_repo(src)
    ws = WorkspaceManager(str(tmp_path / "ws"),
                          projects_base=str(tmp_path / "projects"),
                          code_path_resolver=lambda pid: str(src))
    assert ws.get_project_code_path("p") == src.resolve()
    assert ws.get_project_code_path("p", run_id="R1") == src.resolve()


def test_after_deliver_checks_run_against_the_run_root(tmp_path):
    src = tmp_path / "src"
    base = _init_repo(src)
    wt = tmp_path / "wtA"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "codex/run/A",
                    str(wt), base], cwd=src, check=True)
    seen = {}

    def checker(files=None, project_root: str = "", workspace_root: str = "", **kw):
        seen["dir"] = kw.get("_dir") or workspace_root or project_root
        return {"passed": True}

    sf = SkillFlow(":memory:")
    sf._tool_loader = ToolLoader(_REAL_TOOLS)
    sf._workspace = WorkspaceManager(
        str(tmp_path / "ws"), projects_base=str(tmp_path / "projects"),
        code_path_resolver=lambda pid, run_id=None: str(wt))
    node = StepNode(id="s1", step_type="agent", output_mode="content",
                    output_fixed={"out": "out.txt"},
                    transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name="chk", begin="s1", steps=[node]))
    rid = sf.create_run("chk", {"project_id": "p"}, project_id="p")
    sf.start_run(rid)
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)
    assert sf._workspace.get_project_code_path("p", run_id=rid) == wt.resolve()


def test_read_and_context_roots_follow_the_run(tmp_path):
    """Reads must come from the tree the writes go to."""
    src = tmp_path / "src"
    base = _init_repo(src)
    wt = tmp_path / "wtA"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "codex/run/A",
                    str(wt), base], cwd=src, check=True)
    (wt / "only_here.txt").write_text("run-local\n")

    sf = _sf(tmp_path, lambda pid, run_id=None: str(wt), graph_name="rd")
    rid = sf.create_run("rd", {"project_id": "p"}, project_id="p")
    resolved = sf._workspace.get_project_code_path("p", run_id=rid)
    assert resolved == wt.resolve()
    assert (resolved / "only_here.txt").exists()
    assert not (src / "only_here.txt").exists()
