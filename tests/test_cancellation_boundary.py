"""A stopped run does not commit.

Live, 2026-09-05. An unseeded `coding_impl` run was stopped at 09:09:00 —
`fail_run`, which is the whole of what a stop is. Its already-claimed
`implement` step went on running: two more LLM turns, a complete fresh retry
attempt, an `edit`, and at 09:09:40 `confirm_step` promoted the staged file and
`on_deliver` -> `repo_apply` committed `ac5237b` into a git worktree no maker was
permitted to write to. Forty seconds after the operator was told
"Pipeline stopped."

The fence that should have caught it was already here, in the right place and
with a test of its own (`test_the_fence_fires_before_the_lifecycle_hooks`). It
asks the wrong question. `_assert_epoch` asks *"am I still the executor?"* — and
the answer was yes, correctly: nobody had reclaimed the step. Only the RUN had
ended, and no write path asked about that.

What these pin is a LINEARIZATION, not a preemption, and the difference is the
subject of half the file:

  * A cancellation that commits before `_begin_delivery` prevents every
    lifecycle hook. Nothing is promoted, nothing is committed.
  * A cancellation that commits after it prevents nothing — the hooks are
    already running, `repo_apply` is a git commit, and there is no honest way to
    un-start one. It is reported instead, in `deliveries_in_flight`.

`fail_run` and `_begin_delivery` are each one `BEGIN IMMEDIATE` transaction on
the same connection, so one of those two cases always holds and never both.
"""

import subprocess
import threading
from pathlib import Path

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.exceptions import StaleClaimFenced, TerminalRunFenced
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager

TOOLS = Path(__file__).parent.parent / "src" / "skillflow" / "tools"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    """A real git repository, with a real commit to be unchanged from."""
    repo = tmp_path / "projects" / "p1"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _engine(tmp_path: Path, *, deliver: bool = True) -> SkillFlow:
    node = StepNode(
        id="work", step_type="agent", agent_config="worker",
        output_mode="write", transitions=[Transition(to=None)],
        lifecycle=({"on_deliver": [{"tool": "repo_apply",
                                    "params": {"source_dir": "$STEP_DIR"}}]}
                   if deliver else {}),
    )
    sf = SkillFlow(":memory:", tool_loader=ToolLoader(TOOLS),
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "projects"))
    sf.register_agent_config("worker", tools=["read_file"])
    sf.register_graph(PipelineGraph(name="g", begin="work", steps=[node]))
    return sf


def _claim(sf: SkillFlow):
    run_id = sf.create_run("g", {"project_id": "p1"}, project_id="p1")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    return run_id, sf.claim_next_step(run_id).token


def _stage(sf: SkillFlow, name: str = "DIAGNOSIS.md", body: str = "x\n") -> None:
    """Write what the agent wrote: a file in the step's staging directory."""
    tmp = sf._workspace.get_step_tmp_dir("p1", "g", "work")
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / name).write_text(body, encoding="utf-8")


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD"))


def _step_rows(sf: SkillFlow, run_id: str) -> list[dict]:
    return [dict(r) for r in sf._conn.execute(
        "SELECT id, step_id, status, delivery_started_at FROM skillflow_steps "
        "WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()]


# ── the incident: cancelled before confirm, no commit ────────────────

def test_a_cancelled_run_does_not_commit_to_the_repository(tmp_path):
    """THE test. Real git, real repo_apply, real HEAD.

    Pre-fix this test fails with two commits and a moved HEAD — that inequality
    is `ac5237b`.
    """
    repo = _repo(tmp_path)
    before, n_before = _head(repo), _count(repo)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    _stage(sf)

    report = sf.fail_run(run_id, "stopped by the operator")

    with pytest.raises(TerminalRunFenced):
        sf.confirm_step(token, StepResult(outputs={}))

    assert _head(repo) == before, "a cancelled run committed to the repository"
    assert _count(repo) == n_before
    assert report["steps_closed"] == ["work"]
    assert report["deliveries_in_flight"] == []


def test_nothing_is_promoted_either(tmp_path):
    """after_validate is fenced with on_deliver — the staged output stays staged.

    Promotion is not merely harmless bookkeeping: `{step}/` is what every later
    reader and every re-run sees, so promoting a cancelled step's work publishes
    it to the graph even when no repository is involved.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path, deliver=False)
    run_id, token = _claim(sf)
    _stage(sf)
    final = sf._workspace.get_step_dir("p1", "g", "work")

    sf.fail_run(run_id, "stopped")
    with pytest.raises(TerminalRunFenced):
        sf.confirm_step(token, StepResult(outputs={}))

    assert not (final / "DIAGNOSIS.md").exists()


def test_no_lifecycle_hook_runs_at_all(tmp_path):
    """The mirror of test_the_fence_fires_before_the_lifecycle_hooks."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    _stage(sf)

    ran: list = []
    orig = SkillFlow._execute_lifecycle_hook
    SkillFlow._execute_lifecycle_hook = (
        lambda self, *a, **kw: (ran.append(a[1:]), {"passed": True})[1])
    try:
        sf.fail_run(run_id, "stopped")
        with pytest.raises(TerminalRunFenced):
            sf.confirm_step(token, StepResult(outputs={}))
    finally:
        SkillFlow._execute_lifecycle_hook = orig
    assert ran == [], "a fenced-out executor reached its lifecycle hooks"


def test_a_completed_run_is_fenced_too(tmp_path):
    """`failed` is not the only terminal state; a late confirm on a finished run
    would deliver into a run nobody is watching."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf.complete_run(run_id)
    with pytest.raises(TerminalRunFenced):
        sf.confirm_step(token, StepResult(outputs={}))


# ── the boundary is a linearization, not a preemption ────────────────

def test_a_delivery_already_authorised_is_reported_not_pretended_away(tmp_path):
    """Cancel AFTER `_begin_delivery` committed: the hooks own the step now.

    This is the case the fix does not fix, and saying so is the point. What it
    guarantees instead is that the stop reports it rather than answering
    "Pipeline stopped." over a commit that is about to happen.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    _stage(sf)

    sf._begin_delivery(token)          # as confirm_step does, first thing
    report = sf.fail_run(run_id, "stopped a moment too late")

    assert report["deliveries_in_flight"] == ["work"]
    assert report["steps_closed"] == []
    row = _step_rows(sf, run_id)[0]
    assert row["status"] == "claimed", \
        "an in-flight delivery must not be closed under its own hooks"


def test_cancel_during_a_running_hook_neither_deadlocks_nor_preempts(tmp_path):
    """`fail_run` issued from inside a lifecycle hook, on another thread.

    Two properties at once. It must not DEADLOCK — the hooks run with no
    transaction and no lock held, precisely so a concurrent stop can still take
    `BEGIN IMMEDIATE`. And it must not PREEMPT — the hook was authorised before
    the cancel committed, so the commit it is making lands.
    """
    repo = _repo(tmp_path)
    n_before = _count(repo)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    _stage(sf)

    entered = threading.Event()
    released = threading.Event()
    box: dict = {}
    orig = SkillFlow._execute_lifecycle_hook

    def slow(self, tok, node, hook_name, spec):
        if hook_name == "on_deliver":
            entered.set()
            assert released.wait(10), "fail_run never returned — deadlock"
        return orig(self, tok, node, hook_name, spec)

    def stopper():
        assert entered.wait(10), "the hook never started"
        box["report"] = sf.fail_run(run_id, "stop during on_deliver")
        released.set()

    SkillFlow._execute_lifecycle_hook = slow
    t = threading.Thread(target=stopper, daemon=True)
    try:
        t.start()
        sf.confirm_step(token, StepResult(outputs={}))
    finally:
        SkillFlow._execute_lifecycle_hook = orig
        released.set()
        t.join(10)

    assert not t.is_alive()
    assert box["report"]["deliveries_in_flight"] == ["work"]
    assert _count(repo) == n_before + 1, \
        "an authorised hook was silently prevented — that is not what this fix does"


def test_the_race_is_decided_one_way_or_the_other_never_both(tmp_path):
    """Cancel and deliver from two threads, no artificial ordering.

    Whatever the interleaving, the outcome must be one of exactly two
    consistent states — never a commit that is also reported as prevented, and
    never a prevention that also committed. Repeated so the scheduler has room
    to land in both.
    """
    seen = set()
    for i in range(12):
        root = tmp_path / f"r{i}"
        root.mkdir()
        repo = _repo(root)
        n_before = _count(repo)
        sf = _engine(root)
        run_id, token = _claim(sf)
        _stage(sf)

        box: dict = {}
        go = threading.Barrier(2)

        def deliver():
            go.wait()
            try:
                sf.confirm_step(token, StepResult(outputs={}))
                box["confirm"] = "delivered"
            except TerminalRunFenced:
                box["confirm"] = "fenced"

        def cancel():
            go.wait()
            box["report"] = sf.fail_run(run_id, "race")

        ts = [threading.Thread(target=deliver, daemon=True),
              threading.Thread(target=cancel, daemon=True)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(20)
            assert not t.is_alive(), "deadlock between confirm_step and fail_run"

        committed = _count(repo) - n_before
        report = box["report"]
        if box["confirm"] == "fenced":
            assert committed == 0, "fenced, yet it committed"
            assert report["deliveries_in_flight"] == []
            assert report["steps_closed"] == ["work"]
        else:
            assert committed == 1, "delivered, yet nothing was committed"
            assert report["deliveries_in_flight"] == ["work"]
            assert report["steps_closed"] == []
        seen.add(box["confirm"])
    assert seen, "no iteration ran"


# ── no stranded rows ─────────────────────────────────────────────────

def test_cancelling_closes_the_claim_it_stopped(tmp_path):
    """A claim nothing will ever reclaim must not be left `claimed`.

    The stale-claim reaper refuses to reclaim a claim whose owner PROCESS is
    alive, and on a single-process host the owner is the server itself — so a
    row left `claimed` here is left forever.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)

    report = sf.fail_run(run_id, "stopped")

    rows = _step_rows(sf, run_id)
    assert [r["status"] for r in rows] == ["failed"]
    assert not any(r["status"] == "claimed" for r in rows)
    assert report["steps_closed"] == ["work"]
    err = sf._conn.execute(
        "SELECT last_error FROM skillflow_steps WHERE run_id = ?",
        (run_id,)).fetchone()["last_error"]
    assert "run cancelled before delivery" in err and "stopped" in err


def test_a_fenced_fail_step_does_not_reopen_the_closed_claim(tmp_path):
    """The host's `except` branch calls fail_step; on a cancelled run that would
    put the step back to `pending` and undo the close."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf.fail_run(run_id, "stopped")

    sf.fail_step(token, "boom", retryable=True)      # must not raise

    assert [r["status"] for r in _step_rows(sf, run_id)] == ["failed"]


def test_pending_siblings_stay_pending_and_unclaimable(tmp_path):
    """Spawn prevention already worked; keep it working."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf.fail_run(run_id, "stopped")
    assert sf.claim_next_step(run_id) is None


# ── the cheap boundary: tools ────────────────────────────────────────

def test_agent_tools_are_refused_after_the_run_is_cancelled(tmp_path):
    """`confirm_step` is minutes away on a real agent step; this is seconds."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)

    ok = sf.execute_tool("create", {"file": "probe.md", "content": "a"},
                         run_id=run_id, step_id="work",
                         step_instance_id=token.step_instance_id,
                         claim_epoch=token.claim_epoch)
    assert "error" not in ok, ok

    sf.fail_run(run_id, "stopped")
    refused = sf.execute_tool("create", {"file": "after.md", "content": "b"},
                              run_id=run_id, step_id="work",
                              step_instance_id=token.step_instance_id,
                              claim_epoch=token.claim_epoch)
    assert "error" in refused and "stopped" in refused["error"]


def test_a_refused_tool_writes_nothing(tmp_path):
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf.fail_run(run_id, "stopped")
    out = sf.execute_tool("create", {"file": "leak.md", "content": "no"},
                          run_id=run_id, step_id="work",
                          step_instance_id=token.step_instance_id,
                          claim_epoch=token.claim_epoch)
    assert "error" in out
    tmp = sf._workspace.get_step_tmp_dir("p1", "g", "work")
    assert not (tmp / "leak.md").exists()


# ── the fences stay separate ─────────────────────────────────────────

def test_a_stale_claim_still_reports_a_stale_claim(tmp_path, crash_the_owner):
    """The epoch fence is asked FIRST and keeps its own answer.

    A zombie on a healthy run has lost a race with a peer; that is not the same
    fact as "the run is over", and a host that re-claims on the first must not
    be told the second.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, zombie = _claim(sf)
    crash_the_owner(sf, run_id)
    sf.claim_next_step(run_id)

    with pytest.raises(StaleClaimFenced):
        sf.confirm_step(zombie, StepResult(outputs={}))


def test_a_paused_run_confirms_normally(tmp_path):
    """`paused` is a checkpoint, not an ending. Fencing it would break every
    checkpoint flow in the system."""
    repo = _repo(tmp_path)
    n_before = _count(repo)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    _stage(sf)

    sf.pause_run(run_id)
    sf.confirm_step(token, StepResult(outputs={}))          # must not raise

    assert _count(repo) == n_before + 1
    assert [r["status"] for r in _step_rows(sf, run_id)] == ["completed"]


def test_an_uncancelled_run_delivers_exactly_as_before(tmp_path):
    """The regression that matters most: the fix must be invisible to a healthy
    run."""
    repo = _repo(tmp_path)
    n_before = _count(repo)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    _stage(sf, body="delivered\n")

    sf.confirm_step(token, StepResult(outputs={}))

    assert _count(repo) == n_before + 1
    assert (repo / "DIAGNOSIS.md").read_text(encoding="utf-8") == "delivered\n"
    assert [r["status"] for r in _step_rows(sf, run_id)] == ["completed"]


# ── the launch half: one run, not two ────────────────────────────────

def test_concurrent_get_or_create_run_yields_exactly_one_run(tmp_path):
    """A host that registers a project and then launches it has a poller looking
    at the same (project, graph) pair on another thread. Both used to be able to
    find no run and both to create one.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    ids: list = []
    lock = threading.Lock()
    go = threading.Barrier(6)

    def make():
        go.wait()
        rid = sf.get_or_create_run("g", "p1", {"project_id": "p1"})
        with lock:
            ids.append(rid)

    ts = [threading.Thread(target=make, daemon=True) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
        assert not t.is_alive()

    assert len(set(ids)) == 1, f"competing runs created: {set(ids)}"
    n = sf._conn.execute(
        "SELECT COUNT(*) c FROM skillflow_runs WHERE project_id = 'p1'"
    ).fetchone()["c"]
    assert n == 1
