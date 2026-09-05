"""A stopped run does not commit, and a stop reports what it actually achieved.

Live, 2026-09-05. An unseeded `coding_impl` run was stopped at 09:09:00 —
`fail_run`, which is the whole of what a stop was. Its already-claimed
`implement` step went on running: two more LLM turns, a complete fresh retry
attempt, an `edit`, and at 09:09:40 `confirm_step` promoted the staged file and
`on_deliver` -> `repo_apply` committed `ac5237b` into a git worktree no maker was
permitted to write to. Forty seconds after the operator was told
"Pipeline stopped."

The epoch fence that should have caught it asks the wrong question — "am I still
the executor?", correctly answered yes, because nobody had reclaimed the step.
Only the RUN had ended, and no write path asked about that.

The FIRST repair of this file was rejected by an independent review, and the
reasons shape what is pinned here. It stamped `delivery_started_at` at the top of
`confirm_step`: before validation, before every retry decision, before any hook.
So the stamp survived a return to `pending` and authorised the *next* claim of
the same row; and a cancellation landing between the stamp and the first hook was
reported as an in-flight delivery over a hook that had not started. A timestamp
written early is not proof that anything is running.

What is pinned now is an ADMISSION contract, and its two halves are asserted
separately because they are genuinely different promises:

  stop -> "stopped"   the run is terminal, nothing is admitted, nothing further
                      CAN be admitted. No new effect starts. This is a guarantee.
  stop -> "draining"  the named operations were admitted before the stop
                      committed, cannot be called off, and run to completion. The
                      run terminalises when the last one retires. The stop does
                      NOT claim to have finished.

`admitted` is not `running`, and no test here asserts that it is.
"""

import subprocess
import threading
from pathlib import Path

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.exceptions import StaleClaimFenced, TerminalRunFenced
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader

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
        "SELECT id, step_id, status, claim_epoch FROM skillflow_steps "
        "WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()]


def _ops(sf: SkillFlow, run_id: str) -> list[dict]:
    return [dict(r) for r in sf._conn.execute(
        "SELECT kind, detail, claim_epoch FROM skillflow_active_ops "
        "WHERE run_id = ?", (run_id,)).fetchall()]


def _run(sf: SkillFlow, run_id: str) -> dict:
    return sf.get_run(run_id)


# ── the incident: cancelled before confirm, no commit ────────────────

def test_a_cancelled_run_does_not_commit_to_the_repository(tmp_path):
    """THE test. Real git, real repo_apply, real HEAD.

    Pre-fix this failed with two commits and a moved HEAD — that inequality is
    `ac5237b`.
    """
    repo = _repo(tmp_path)
    before, n_before = _head(repo), _count(repo)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    _stage(sf)

    report = sf.stop_run(run_id, "stopped by the operator")

    with pytest.raises(TerminalRunFenced):
        sf.confirm_step(token, StepResult(outputs={}))

    assert _head(repo) == before, "a cancelled run committed to the repository"
    assert _count(repo) == n_before
    assert report["outcome"] == "stopped"
    assert report["steps_closed"] == ["work"]
    assert report["admitted_operations"] == []


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

    sf.stop_run(run_id, "stopped")
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
        sf.stop_run(run_id, "stopped")
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


# ── admission is not "running", and the report says so ───────────────

def test_an_admitted_delivery_is_reported_as_admitted_not_as_in_flight(tmp_path):
    """Cancel AFTER admission: the operation owns the step now.

    This is the case the fix does not fix, and saying so is the point. The stop
    does not answer "stopped"; it answers "draining" and names what it could not
    call off.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    _stage(sf)

    op = sf._admit_op("delivery", run_id,
                      step_instance_id=token.step_instance_id,
                      claim_epoch=token.claim_epoch, detail=token.step_id)
    report = sf.stop_run(run_id, "stopped a moment too late")

    assert report["outcome"] == "draining"
    assert report["admitted_operations"] == ["delivery:work"]
    assert report["steps_closed"] == []
    assert _step_rows(sf, run_id)[0]["status"] == "claimed", \
        "an admitted operation must not be closed under itself"
    assert _run(sf, run_id)["status"] == "running", \
        "a draining run is not terminal yet and must not claim to be"
    assert _run(sf, run_id)["cancel_requested_at"], \
        "the requested cancellation must be visible while it drains"

    sf._retire_op(op)                       # the operation ends

    assert _run(sf, run_id)["status"] == "failed", \
        "the drain must terminalise when the last admitted operation retires"
    assert _step_rows(sf, run_id)[0]["status"] == "failed"
    assert _ops(sf, run_id) == []


def test_cancel_immediately_after_admission_before_any_hook(tmp_path):
    """The reviewer's HOOK_GAP interleaving, as a regression test.

    Cancel between the admission commit and the first lifecycle hook. The commit
    lands — that is the admitted operation finishing, and it is honest — but the
    stop must NOT report `stopped`, and the run must terminalise afterwards.
    """
    repo = _repo(tmp_path)
    n_before = _count(repo)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    _stage(sf)

    seen: dict = {}
    real_admit = sf._admit_op

    def admit_then_cancel(*a, **kw):
        op = real_admit(*a, **kw)
        seen["report"] = sf.stop_run(run_id, "cancel before first lifecycle hook")
        return op
    sf._admit_op = admit_then_cancel

    sf.confirm_step(token, StepResult(outputs={}))

    assert seen["report"]["outcome"] == "draining"
    assert seen["report"]["admitted_operations"] == ["delivery:work"]
    assert seen["report"]["steps_closed"] == []
    assert _count(repo) == n_before + 1
    assert _run(sf, run_id)["status"] == "failed", \
        "the drain never completed — the run would hang cancelled forever"
    assert _ops(sf, run_id) == []


def test_cancel_just_before_admission_prevents_the_commit(tmp_path):
    """The other side of the same boundary, and the one that is a guarantee."""
    repo = _repo(tmp_path)
    n_before = _count(repo)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    _stage(sf)

    seen: dict = {}
    real_admit = sf._admit_op

    def cancel_then_admit(*a, **kw):
        seen["report"] = sf.stop_run(run_id, "cancel before admission")
        return real_admit(*a, **kw)
    sf._admit_op = cancel_then_admit

    with pytest.raises(TerminalRunFenced):
        sf.confirm_step(token, StepResult(outputs={}))

    assert seen["report"]["outcome"] == "stopped"
    assert seen["report"]["steps_closed"] == ["work"]
    assert _count(repo) == n_before, "a stop that reported `stopped` still committed"


def test_the_race_is_decided_one_way_or_the_other_never_both(tmp_path):
    """Confirm and cancel from two threads, no artificial ordering.

    Whatever the interleaving, the outcome must be one of exactly two consistent
    states — never a commit that is also reported as prevented, and never a
    prevention that also committed.
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
            box["report"] = sf.stop_run(run_id, "race")

        ts = [threading.Thread(target=deliver, daemon=True),
              threading.Thread(target=cancel, daemon=True)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(20)
            assert not t.is_alive(), "deadlock between confirm_step and stop_run"

        committed = _count(repo) - n_before
        report = box["report"]
        if box["confirm"] == "fenced":
            assert committed == 0, "fenced, yet it committed"
            assert report["outcome"] == "stopped"
            assert report["admitted_operations"] == []
            assert report["steps_closed"] == ["work"]
        else:
            assert committed == 1, "delivered, yet nothing was committed"
            assert report["outcome"] == "draining"
            assert report["admitted_operations"] == ["delivery:work"]
            assert report["steps_closed"] == []
        # Either way the run must end up terminal with nothing admitted.
        assert _run(sf, run_id)["status"] == "failed"
        assert _ops(sf, run_id) == []
        seen.add(box["confirm"])
    assert seen, "no iteration ran"


def test_cancel_during_a_running_hook_neither_deadlocks_nor_preempts(tmp_path):
    """`stop_run` issued from inside a lifecycle hook, on another thread.

    It must not DEADLOCK — the hooks run with no transaction and no lock held,
    precisely so a concurrent stop can still take `BEGIN IMMEDIATE`. And it must
    not PREEMPT — the operation was admitted before the cancel committed, so the
    commit it is making lands.
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
            assert released.wait(10), "stop_run never returned — deadlock"
        return orig(self, tok, node, hook_name, spec)

    def stopper():
        assert entered.wait(10), "the hook never started"
        box["report"] = sf.stop_run(run_id, "stop during on_deliver")
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
    assert box["report"]["outcome"] == "draining"
    assert box["report"]["admitted_operations"] == ["delivery:work"]
    assert _count(repo) == n_before + 1, \
        "an admitted hook was silently prevented — that is not what this does"
    assert _run(sf, run_id)["status"] == "failed"


# ── the sticky-authorisation defect ──────────────────────────────────

def test_an_empty_confirm_that_retries_leaves_no_authorisation_behind(tmp_path):
    """The reviewer's STICKY interleaving, through real APIs only.

    A confirm with nothing staged takes the existing "promoted no files" re-ask
    path: the row goes back to `pending` and is claimed again. The first repair
    stamped authorisation at the top of `confirm_step`, so the stamp survived
    that reset and the FRESH claim inherited it — a cancellation then called an
    attempt that had never entered a hook an in-flight delivery, and left its
    claim open forever.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)

    sf.confirm_step(token, StepResult(outputs={}))       # nothing staged

    rows = _step_rows(sf, run_id)
    assert rows[0]["status"] == "pending", rows
    assert _ops(sf, run_id) == [], \
        "the retry kept an authorisation from an attempt that ended"

    fresh = sf.claim_next_step(run_id)
    assert fresh is not None
    assert fresh.token.claim_epoch > token.claim_epoch

    report = sf.stop_run(run_id, "cancel fresh retry before confirm")

    assert report["outcome"] == "stopped"
    assert report["steps_closed"] == ["work"]
    assert report["admitted_operations"] == []
    assert [r["status"] for r in _step_rows(sf, run_id)] == ["failed"], \
        "a fresh retry claim was left stranded by the stop"


def test_a_reclaim_does_not_erase_a_still_running_operation(tmp_path):
    """The trap the first draft fell into, and the reason it is a trap.

    Deleting an older claim's admission when the row is re-claimed looks like
    tidy-up. It is not: bumping the epoch revokes FUTURE admission, it cannot
    stop an operation that is already running. A disconnected executor can still
    be alive and a hook can hold a subprocess. So the record must survive the
    re-claim, and the stop must keep saying `draining` until the operation
    actually retires — otherwise it is the original false stop with the evidence
    deleted.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    op = sf._admit_op("delivery", run_id,
                      step_instance_id=token.step_instance_id,
                      claim_epoch=token.claim_epoch, detail="work")

    sf.release_claim(token, "executor disconnected — but is still running")
    fresh = sf.claim_next_step(run_id)
    assert fresh is not None and fresh.token.claim_epoch > token.claim_epoch
    assert len(_ops(sf, run_id)) == 1, \
        "the re-claim erased a record for work that never stopped"

    report = sf.stop_run(run_id, "stopped")
    assert report["outcome"] == "draining", report
    assert report["admitted_operations"] == ["delivery:work"]
    assert _run(sf, run_id)["status"] == "running"

    sf._retire_op(op)                       # the old operation really ends
    assert _run(sf, run_id)["status"] == "failed"
    assert [r["status"] for r in _step_rows(sf, run_id)] == ["failed"]


def test_a_late_retirement_cannot_delete_another_operations_record(tmp_path):
    """Retirement is by the operation's own id, never by step or epoch."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    a = sf._admit_op("tool", run_id, step_instance_id=token.step_instance_id,
                     claim_epoch=token.claim_epoch, detail="first")
    b = sf._admit_op("tool", run_id, step_instance_id=token.step_instance_id,
                     claim_epoch=token.claim_epoch, detail="second")

    sf._retire_op(a)
    assert [r["detail"] for r in _ops(sf, run_id)] == ["second"]
    sf._retire_op(a)                        # late duplicate retirement
    assert [r["detail"] for r in _ops(sf, run_id)] == ["second"]

    assert sf.stop_run(run_id, "stopped")["outcome"] == "draining"
    sf._retire_op(b)
    assert _run(sf, run_id)["status"] == "failed"


# ── liveness: the only thing that may retire someone else's operation ─

def _make_owner_dead(sf, run_id):
    """Rewrite the op's owner to a provably-absent process — our own pid with a
    process-start marker that is not ours, exactly as the crash fixture does."""
    from skillflow.identity import worker_identity
    gone = f"{worker_identity('delivery')} start=gone"
    with sf._lock:
        sf._conn.execute(
            "UPDATE skillflow_active_ops SET owner = ? WHERE run_id = ?",
            (gone, run_id))
        sf._conn.commit()


def test_a_dead_owner_alone_cannot_mark_the_run_terminal(tmp_path):
    """The one that was wrong before: owner death is not effect quiescence.

    `owner_is_dead` proves the owner PROCESS ended. The contract is about
    EFFECTS, and `repo_apply` runs `git add` / `git commit` through
    `subprocess.run` — a child can outlive the parent that spawned it. Retiring
    the record on owner death therefore let the last one disappear and the
    cancellation terminalise, reporting `stopped` (which means *no remaining
    effects*) while a commit could still be landing.

    So the record SURVIVES, the cancellation stays pending, and the state is
    reported. No child process is spawned or observed here; the assertion is
    that the engine does not treat a dead owner as proof.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf._admit_op("delivery", run_id, step_instance_id=token.step_instance_id,
                 claim_epoch=token.claim_epoch, detail="work")
    assert sf.stop_run(run_id, "stopped")["outcome"] == "draining"

    _make_owner_dead(sf, run_id)
    out = sf.audit_operation_owners(run_id)

    assert out["lost"] == ["delivery:work"] and out["unknown"] == []
    assert out["recovery_required"] is True and out["hint"]
    assert len(_ops(sf, run_id)) == 1, \
        "the audit retired a record on owner death alone"
    assert _run(sf, run_id)["status"] == "running", \
        "a dead owner completed a cancellation without proving its effects ended"
    assert [r["status"] for r in _step_rows(sf, run_id)] == ["claimed"]


def test_the_lost_owner_is_recorded_and_the_audit_is_idempotent(tmp_path):
    """A lost owner may need attention, and that is the truthful answer — so it
    is durable, not a log line that scrolls away."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf._admit_op("delivery", run_id, step_instance_id=token.step_instance_id,
                 claim_epoch=token.claim_epoch, detail="work")
    _make_owner_dead(sf, run_id)

    first = sf.audit_operation_owners(run_id)
    stamped = sf._conn.execute(
        "SELECT owner_lost_at FROM skillflow_active_ops WHERE run_id = ?",
        (run_id,)).fetchone()["owner_lost_at"]
    assert stamped, "the observation was not recorded"
    second = sf.audit_operation_owners(run_id)

    assert first["lost"] == second["lost"] == ["delivery:work"]
    assert sf._conn.execute(
        "SELECT owner_lost_at FROM skillflow_active_ops WHERE run_id = ?",
        (run_id,)).fetchone()["owner_lost_at"] == stamped
    assert len(_ops(sf, run_id)) == 1


def test_a_stop_reports_that_it_will_not_complete_on_its_own(tmp_path):
    """`recovery_required` is the field that stops "draining" from reading as
    "wait and it will finish"."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf._admit_op("delivery", run_id, step_instance_id=token.step_instance_id,
                 claim_epoch=token.claim_epoch, detail="work")
    _make_owner_dead(sf, run_id)

    report = sf.stop_run(run_id, "stopped")

    assert report["outcome"] == "draining"
    assert report["admitted_owner_state"] == {"delivery:work": "dead"}
    assert report["recovery_required"] is True
    assert "EFFECTS have stopped" in report["recovery_hint"]


def test_ordinary_completion_still_drains_and_terminalises(tmp_path):
    """The normal path is unchanged and is the real completion: an operation
    that finishes retires itself in `finally`, and the last one ends the run."""
    repo = _repo(tmp_path)
    n_before = _count(repo)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    _stage(sf)

    seen: dict = {}
    real_admit = sf._admit_op

    def admit_then_cancel(*a, **kw):
        op = real_admit(*a, **kw)
        seen["report"] = sf.stop_run(run_id, "cancel after admission")
        return op
    sf._admit_op = admit_then_cancel

    sf.confirm_step(token, StepResult(outputs={}))     # runs to completion

    assert seen["report"]["outcome"] == "draining"
    assert seen["report"]["recovery_required"] is False
    assert _count(repo) == n_before + 1
    assert _ops(sf, run_id) == []
    assert _run(sf, run_id)["status"] == "failed", \
        "an operation that really finished did not complete the cancellation"


def test_only_an_explicit_release_with_evidence_retires_a_lost_operation(tmp_path):
    """The one path other than the operation's own `finally`, and it is manual.

    The evidence is recorded, not validated — skillflow cannot see a git child it
    never spawned, and pretending to check would be the same mistake again. What
    it buys is attribution: a run that ended this way names the attestation it
    ended on, instead of a supervisor quietly deciding a dead pid meant a
    finished commit.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    op = sf._admit_op("delivery", run_id,
                      step_instance_id=token.step_instance_id,
                      claim_epoch=token.claim_epoch, detail="work")
    sf.stop_run(run_id, "stopped")
    _make_owner_dead(sf, run_id)
    sf.audit_operation_owners(run_id)
    assert _run(sf, run_id)["status"] == "running"

    with pytest.raises(ValueError):
        sf.release_operation(op, evidence="")          # no silent force-complete
    assert len(_ops(sf, run_id)) == 1
    assert _run(sf, run_id)["status"] == "running"

    out = sf.release_operation(
        op, evidence="git status settles, no .git/index.lock, no writer on the "
                     "staging dir — checked by hand at 11:20Z")

    assert out["released"] is True and out["run_status"] == "failed"
    assert _ops(sf, run_id) == []
    assert [r["status"] for r in _step_rows(sf, run_id)] == ["failed"]


def test_releasing_an_unknown_operation_is_a_no_op(tmp_path):
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, _ = _claim(sf)
    out = sf.release_operation(9999, evidence="nothing is running")
    assert out["released"] is False
    assert _run(sf, run_id)["status"] == "running"


def test_a_live_owner_is_never_marked_lost(tmp_path):
    """Negative control. Age, a changed epoch and a released claim are all
    compatible with the operation still running, and none of them may mark it."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    op = sf._admit_op("delivery", run_id,
                      step_instance_id=token.step_instance_id,
                      claim_epoch=token.claim_epoch, detail="work")
    with sf._lock:                      # make it look old
        sf._conn.execute(
            "UPDATE skillflow_active_ops SET admitted_at = '2000-01-01 00:00:00'")
        sf._conn.commit()
    sf.release_claim(token, "epoch churn")
    sf.claim_next_step(run_id)
    sf.stop_run(run_id, "stopped")

    out = sf.audit_operation_owners(run_id)

    assert out["lost"] == [] and out["unknown"] == [] and out["alive"] == 1
    assert out["recovery_required"] is False
    assert len(_ops(sf, run_id)) == 1
    assert _run(sf, run_id)["status"] == "running", \
        "a live operation was declared gone and the stop reported completion"
    sf._retire_op(op)
    assert _run(sf, run_id)["status"] == "failed"


def test_an_unobservable_owner_is_reported_not_assumed_gone(tmp_path):
    """`owner_is_dead` is three-valued and `None` means *cannot observe*. That is
    a state an operator must be shown, never a licence to declare success."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf._admit_op("delivery", run_id, step_instance_id=token.step_instance_id,
                 claim_epoch=token.claim_epoch, detail="work")
    with sf._lock:
        sf._conn.execute("UPDATE skillflow_active_ops SET owner = 'worker'")
        sf._conn.commit()

    report = sf.stop_run(run_id, "stopped")
    assert report["admitted_owner_state"] == {"delivery:work": "unknown"}
    assert report["recovery_required"] is True

    out = sf.audit_operation_owners(run_id)
    assert out["unknown"] == ["delivery:work"] and out["lost"] == []
    assert len(_ops(sf, run_id)) == 1
    assert _run(sf, run_id)["status"] == "running"


def test_the_audit_sweeps_every_run_when_given_none(tmp_path):
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf._admit_op("delivery", run_id, step_instance_id=token.step_instance_id,
                 claim_epoch=token.claim_epoch, detail="work")
    sf.stop_run(run_id, "stopped")
    _make_owner_dead(sf, run_id)
    out = sf.audit_operation_owners()
    assert out["lost"] == ["delivery:work"]
    assert _run(sf, run_id)["status"] == "running"


# ── the inline tool-step path ────────────────────────────────────────

def _tool_step_engine(tmp_path):
    """A graph whose FIRST node is an inline tool step — the advance_run fast
    path, which never goes through `execute_tool`."""
    node = StepNode(id="apply", step_type="tool", tool_name="repo_apply",
                    tool_params={"source_dir": "$STEP_DIR"},
                    transitions=[Transition(to=None)])
    sf = SkillFlow(":memory:", tool_loader=ToolLoader(TOOLS),
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "projects"))
    sf.register_graph(PipelineGraph(name="tg", begin="apply", steps=[node]))
    run_id = sf.create_run("tg", {"project_id": "p1"}, project_id="p1")
    sf.start_run(run_id)
    return sf, run_id


def test_an_inline_tool_step_is_refused_after_a_stop(tmp_path):
    """A tool NODE is a real side effect (git_sync_pre, repo_apply, run_tests)
    and it never passes through `execute_tool`. Guarding it with a status read
    would be the same check-then-act; it is admitted like everything else."""
    repo = _repo(tmp_path)
    sf, run_id = _tool_step_engine(tmp_path)
    n_before = _count(repo)

    assert sf.stop_run(run_id, "stopped")["outcome"] == "stopped"
    sf.advance_run(run_id)

    assert _count(repo) == n_before, "an inline tool step ran after the stop"
    assert _ops(sf, run_id) == []


def test_an_inline_tool_step_admits_and_retires(tmp_path):
    """And on a healthy run it takes and releases its admission, so a stop
    arriving mid-execution is told `draining` rather than `stopped`."""
    _repo(tmp_path)
    sf, run_id = _tool_step_engine(tmp_path)
    seen: dict = {}
    real = sf._execute_tool_inline

    def cancel_then_run(*a, **kw):
        seen["report"] = sf.stop_run(run_id, "cancel mid tool step")
        return real(*a, **kw)
    sf._execute_tool_inline = cancel_then_run

    sf.advance_run(run_id)

    assert seen["report"]["outcome"] == "draining"
    assert seen["report"]["admitted_operations"] == ["tool_step:repo_apply"]
    assert _ops(sf, run_id) == [], "the tool step never retired its admission"
    assert _run(sf, run_id)["status"] == "failed"


# ── no stranded rows, and no revival of a closed one ─────────────────

def test_cancelling_closes_the_claim_it_stopped(tmp_path):
    """A claim nothing will ever reclaim must not be left `claimed`.

    The stale-claim reaper refuses to reclaim a claim whose owner PROCESS is
    alive, and on a single-process host the owner is the server itself — so a
    row left `claimed` here is left forever.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)

    report = sf.stop_run(run_id, "stopped")

    rows = _step_rows(sf, run_id)
    assert [r["status"] for r in rows] == ["failed"]
    assert report["steps_closed"] == ["work"]
    err = sf._conn.execute(
        "SELECT last_error FROM skillflow_steps WHERE run_id = ?",
        (run_id,)).fetchone()["last_error"]
    assert "run cancelled" in err and "stopped" in err


def test_a_fenced_fail_step_does_not_reopen_the_closed_claim(tmp_path):
    """The host's `except` branch calls fail_step; on a cancelled run that would
    put the step back to `pending` and undo the close."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf.stop_run(run_id, "stopped")

    sf.fail_step(token, "boom", retryable=True)      # must not raise

    assert [r["status"] for r in _step_rows(sf, run_id)] == ["failed"]


def test_fail_step_loses_an_ordered_race_with_the_cancellation(tmp_path):
    """The reviewer's FAIL_STEP_RACE interleaving, as a regression test.

    `fail_step` used to read the run status, and only then open the transaction
    that mutates the row. A cancellation committing in that gap closed the claim
    and `_fail_step_in_tx` reset the closed row to `pending` — it re-reads
    `version` from the row it is about to write, so it cannot notice on its own.
    The cancel is fired from `_release_step_tools`, which is the last thing
    `fail_step` does before opening its transaction.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)

    box: dict = {}
    real_release = sf._release_step_tools

    def release_then_cancel(*a, **kw):
        real_release(*a, **kw)
        if "report" not in box:
            box["report"] = sf.stop_run(run_id, "cancel after fail_step began")
    sf._release_step_tools = release_then_cancel

    sf.fail_step(token, "error", retryable=True)

    assert box["report"]["outcome"] == "stopped"
    assert box["report"]["steps_closed"] == ["work"]
    assert [r["status"] for r in _step_rows(sf, run_id)] == ["failed"], \
        "fail_step revived a claim the cancellation had closed"


def test_pending_siblings_stay_pending_and_unclaimable(tmp_path):
    """Spawn prevention already worked; keep it working."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf.stop_run(run_id, "stopped")
    assert sf.claim_next_step(run_id) is None


def test_a_draining_run_claims_no_new_step(tmp_path):
    """`draining` is not `running` for the purpose of starting work. The run row
    still says `running` — it genuinely is — so the guard is the requested-cancel
    column, and this is what proves it is honoured."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf._admit_op("delivery", run_id, step_instance_id=token.step_instance_id,
                 claim_epoch=token.claim_epoch, detail="work")
    report = sf.stop_run(run_id, "stopped")
    assert report["outcome"] == "draining"
    assert _run(sf, run_id)["status"] == "running"

    sf.release_claim(token, "make the row claimable again")
    assert sf.claim_next_step(run_id) is None, \
        "a draining run handed out a new claim"


def test_stopping_a_terminal_run_says_so(tmp_path):
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, _ = _claim(sf)
    sf.stop_run(run_id, "first")
    again = sf.stop_run(run_id, "second")
    assert again["outcome"] == "already_terminal"


# ── the cheap boundary: tools ────────────────────────────────────────

def test_a_terminal_stop_is_never_followed_by_a_tool_write(tmp_path):
    """The reviewer's TOOL_RACE finding, stated as the guarantee it broke.

    The stop reported `steps_closed=['work']` and the `create` tool then staged a
    file anyway — a brand-new side effect after a completed stop, not a running
    one being preempted. Admission and the status check are one transaction now,
    so a stop that answers `stopped` cannot be followed by an admitted tool.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)

    ok = sf.execute_tool("create", {"file": "probe.md", "content": "a"},
                         run_id=run_id, step_id="work",
                         step_instance_id=token.step_instance_id,
                         claim_epoch=token.claim_epoch)
    assert "error" not in ok, ok

    report = sf.stop_run(run_id, "stopped")
    assert report["outcome"] == "stopped"

    refused = sf.execute_tool("create", {"file": "after.md", "content": "b"},
                              run_id=run_id, step_id="work",
                              step_instance_id=token.step_instance_id,
                              claim_epoch=token.claim_epoch)
    assert "error" in refused
    tmp = sf._workspace.get_step_tmp_dir("p1", "g", "work")
    assert not (tmp / "after.md").exists(), \
        "a new file was staged after the stop reported the claim closed"


def test_a_tool_admitted_before_the_stop_finishes_and_the_stop_says_draining(
        tmp_path):
    """The other side, from inside the tool: a cancel arriving after the tool was
    admitted cannot call it off, and the report must not pretend otherwise."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)

    box: dict = {}
    real_impl = sf._execute_tool_impl

    def cancel_then_run(*a, **kw):
        box["report"] = sf.stop_run(run_id, "cancel after the tool was admitted")
        return real_impl(*a, **kw)
    sf._execute_tool_impl = cancel_then_run

    out = sf.execute_tool("create", {"file": "admitted.md", "content": "c"},
                          run_id=run_id, step_id="work",
                          step_instance_id=token.step_instance_id,
                          claim_epoch=token.claim_epoch)

    assert "error" not in out, out
    assert box["report"]["outcome"] == "draining"
    assert box["report"]["admitted_operations"] == ["tool:create"]
    tmp = sf._workspace.get_step_tmp_dir("p1", "g", "work")
    assert (tmp / "admitted.md").exists()
    assert _run(sf, run_id)["status"] == "failed", \
        "the tool retired but the drain never completed"
    assert _ops(sf, run_id) == []


def test_a_tool_is_refused_while_the_run_drains(tmp_path):
    """Nothing NEW starts once a cancellation has been requested, even before it
    has finished draining."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    op = sf._admit_op("delivery", run_id,
                      step_instance_id=token.step_instance_id,
                      claim_epoch=token.claim_epoch, detail="work")
    assert sf.stop_run(run_id, "stopped")["outcome"] == "draining"

    out = sf.execute_tool("create", {"file": "during_drain.md", "content": "d"},
                          run_id=run_id, step_id="work",
                          step_instance_id=token.step_instance_id,
                          claim_epoch=token.claim_epoch)
    assert "error" in out and "draining" in out["error"]
    tmp = sf._workspace.get_step_tmp_dir("p1", "g", "work")
    assert not (tmp / "during_drain.md").exists()
    sf._retire_op(op)


def test_a_tool_retires_its_admission_even_when_it_raises(tmp_path):
    """An admission that outlives its operation is an authorisation nobody
    holds, and the run could never terminalise."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)

    def boom(*a, **kw):
        raise RuntimeError("tool exploded")
    sf._execute_tool_impl = boom

    with pytest.raises(RuntimeError):
        sf.execute_tool("create", {"file": "x.md", "content": "y"},
                        run_id=run_id, step_id="work",
                        step_instance_id=token.step_instance_id,
                        claim_epoch=token.claim_epoch)
    assert _ops(sf, run_id) == []
    assert sf.stop_run(run_id, "stopped")["outcome"] == "stopped"


def test_a_delivery_retires_its_admission_on_the_empty_delivery_path(tmp_path):
    """The re-ask path returns from the middle of the lifecycle block."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    sf.confirm_step(token, StepResult(outputs={}))       # nothing staged
    assert _ops(sf, run_id) == []


# ── the fences stay separate ─────────────────────────────────────────

def test_a_stale_claim_still_reports_a_stale_claim(tmp_path, crash_the_owner):
    """The epoch fence is asked FIRST and keeps its own answer.

    A zombie on a healthy run has lost a race with a peer; that is not the same
    fact as "the run is over", and a host that re-claims on the first must not be
    told the second.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, zombie = _claim(sf)
    crash_the_owner(sf, run_id)
    sf.claim_next_step(run_id)

    with pytest.raises(StaleClaimFenced):
        sf.confirm_step(zombie, StepResult(outputs={}))


def test_a_zombie_cannot_be_admitted_even_if_it_reaches_the_hooks(tmp_path,
                                                                  crash_the_owner):
    """Admission re-checks the epoch inside its own transaction, so a claim
    superseded after `_assert_epoch` is still refused."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, zombie = _claim(sf)
    crash_the_owner(sf, run_id)
    sf.claim_next_step(run_id)

    with pytest.raises(StaleClaimFenced):
        sf._admit_op("delivery", run_id,
                     step_instance_id=zombie.step_instance_id,
                     claim_epoch=zombie.claim_epoch, detail="work")


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
    run, and must leave nothing behind."""
    repo = _repo(tmp_path)
    n_before = _count(repo)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    _stage(sf, body="delivered\n")

    sf.confirm_step(token, StepResult(outputs={}))

    assert _count(repo) == n_before + 1
    assert (repo / "DIAGNOSIS.md").read_text(encoding="utf-8") == "delivered\n"
    assert [r["status"] for r in _step_rows(sf, run_id)] == ["completed"]
    assert _ops(sf, run_id) == []
    assert _run(sf, run_id)["cancel_requested_at"] is None


# ── the launch half: one run per instance ────────────────────────────

def test_concurrent_get_or_create_run_yields_one_run_per_instance(tmp_path):
    """A host that registers a project and then launches it has a poller looking
    at the same (project, graph) pair on another thread. Both used to be able to
    find no run and both to create one.

    SCOPE, stated because the first report overclaimed it: this serialises the
    threads sharing ONE SkillFlow instance's re-entrant lock. It is not
    database-wide uniqueness and does not cover a second instance or a second
    process.
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


# ── API compatibility ────────────────────────────────────────────────

def test_fail_run_still_exists_and_carries_the_drain_semantics(tmp_path):
    """Every host calls `fail_run`; it is kept and delegates to `stop_run`, so a
    caller that was never updated gets the safe behaviour rather than the old
    status flip. Pinned rather than asserted in prose."""
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, token = _claim(sf)
    op = sf._admit_op("delivery", run_id,
                      step_instance_id=token.step_instance_id,
                      claim_epoch=token.claim_epoch, detail=token.step_id)

    report = sf.fail_run(run_id, "stopped through the old entry point")

    assert report["outcome"] == "draining"
    assert report["admitted_operations"] == ["delivery:work"]
    assert _run(sf, run_id)["status"] == "running"
    sf._retire_op(op)
    assert _run(sf, run_id)["status"] == "failed"


def test_the_internal_failure_paths_still_fail_immediately(tmp_path):
    """`_fail_run_in_tx` stays the IMMEDIATE writer for the engine's own failures
    (cycle limit, routing dead end, tool-step failure). Those are not operator
    stops: there is nothing external to drain and the caller is usually already
    inside a transaction. The asymmetry is deliberate and this is what says so.
    """
    _repo(tmp_path)
    sf = _engine(tmp_path)
    run_id, _ = _claim(sf)
    with sf._tx() as conn:
        sf._fail_run_in_tx(conn, run_id, "routing dead end")
    assert _run(sf, run_id)["status"] == "failed"
    assert _run(sf, run_id)["cancel_requested_at"] is None
