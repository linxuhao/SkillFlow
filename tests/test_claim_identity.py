"""A dead worker is not a quiet one, and a reclaimed one cannot keep writing.

Three properties, one cause. `claimed_by` used to be the literal "worker", so
"the owner crashed" and "the owner is alive but silent" were the same row, and
only the silence lease could answer either — which meant it answered BOTH, and
answered the second one wrong: an agent step sitting inside a ten-minute LLM
call traces nothing while it waits, and got reaped for working. And the
optimistic `version` guard sat at the BOTTOM of confirm_step, after on_deliver
had already run repo_apply against the user's git repo — so a prematurely
reclaimed executor committed beside its replacement and was told it had lost
the step afterwards.

So: the claim names its owner; the owner's liveness — not the clock — decides
whether it is recovered; and whoever loses a claim is fenced out of writing.
The fence still matters with ownership deciding, because ownership cannot
always answer (a legacy row, another kernel boot, no /proc) and the host has
reset paths of its own (startup recovery, checkpoint rejection).
"""

import os
import sqlite3
import subprocess
import sys
import time

import pytest

from skillflow import identity
from skillflow.core import SkillFlow, StepResult
from skillflow.exceptions import StaleClaimFenced
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.identity import owner_is_dead, parse_identity, worker_identity


def _one_step_run(sf: SkillFlow, step: StepNode | None = None,
                  name: str = "test") -> str:
    node = step or StepNode(id="a", step_type="agent",
                            transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name=name, begin=node.id, steps=[node]))
    run_id = sf.create_run(name)
    sf.start_run(run_id)
    sf.advance_run(run_id)
    return run_id


def _claimed_by(sf: SkillFlow, run_id: str) -> str:
    return sf._conn.execute(
        "SELECT claimed_by FROM skillflow_steps WHERE run_id = ? "
        "ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()["claimed_by"]


def _insert_claim(sf: SkillFlow, run_id: str, step_id: str, claimed_by: str,
                  age_seconds: float = 0.0) -> int:
    """Plant a claimed step owned by `claimed_by`, claimed `age_seconds` ago."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                       time.gmtime(time.time() - age_seconds))
    sqlite_ts = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.gmtime(time.time() - age_seconds))
    with sf._lock:
        cur = sf._conn.execute(
            "INSERT INTO skillflow_steps (run_id, step_id, step_config_json, "
            "inputs_json, max_retries, status, claimed_at, claimed_by, "
            "claim_epoch, created_at, updated_at) "
            "VALUES (?, ?, '{}', '{}', 3, 'claimed', ?, ?, 1, ?, ?)",
            (run_id, step_id, ts, claimed_by, sqlite_ts, sqlite_ts))
        sf._conn.commit()
        return cur.lastrowid


def _status(sf: SkillFlow, step_row_id: int) -> str:
    return sf._conn.execute(
        "SELECT status FROM skillflow_steps WHERE id = ?",
        (step_row_id,)).fetchone()["status"]


@pytest.fixture
def fake_self(monkeypatch):
    """Pin this process's identity so the tests read the same on any platform.

    `boot` must be non-None for the liveness path to engage at all — on a
    kernel without /proc the real value is None and every answer is "unknown".
    """
    me = {"host": "testhost", "pid": os.getpid(), "boot": "testboot",
          "ns": "4026531836", "start": None}
    monkeypatch.setattr(identity, "_self_identity", lambda: me)
    return me


@pytest.fixture
def dead_pid():
    """A pid that is definitely gone: spawned, exited, and reaped."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


# ── Change 1: the claim records who made it ─────────────────────────

def test_a_claim_records_a_resolvable_identity(sf: SkillFlow):
    """`claimed_by` names a process, not a role. Without this the reaper has
    nothing to ask a question about."""
    run_id = _one_step_run(sf)
    claimed = sf.claim_next_step(run_id)
    assert claimed is not None

    ident = parse_identity(_claimed_by(sf, run_id))
    assert ident is not None, "claim wrote no identity"
    assert ident["role"] == "worker"
    assert ident["pid"] == os.getpid()
    assert ident["host"]
    # The owner is this very process, so it is certainly not dead.
    assert owner_is_dead(_claimed_by(sf, run_id)) is not True


def test_the_identity_survives_a_round_trip(fake_self):
    ident = parse_identity(worker_identity("tool-inline"))
    assert ident["role"] == "tool-inline"
    assert ident["pid"] == os.getpid()
    assert ident["boot"] == "testboot"


def test_a_legacy_literal_carries_no_identity():
    """Rows written before this change must not break the reaper: they simply
    have no owner to probe, and keep the lease as their only recovery path."""
    for legacy in ("worker", "tool-inline", "", None):
        assert parse_identity(legacy) is None
        assert owner_is_dead(legacy) is None


# ── Change 1: death is faster than silence ──────────────────────────

def test_a_dead_owner_is_reclaimed_without_waiting_for_the_lease(
        sf: SkillFlow, fake_self, dead_pid):
    run_id = _one_step_run(sf)
    row_id = _insert_claim(
        sf, run_id, "a",
        f"worker host=testhost pid={dead_pid} boot=testboot")

    # Claimed a second ago against an hour-long lease: silence says "alive",
    # the pid says otherwise, and the pid is the one that knows.
    assert run_id in sf.recover_stale_claims(stale_threshold_seconds=3600)
    assert _status(sf, row_id) == "pending"


def test_a_dead_owner_beats_even_a_no_timeout_tool(sf: SkillFlow, fake_self,
                                                   dead_pid):
    """`timeout_seconds: 0` means "never stale" because reclaiming a LIVE tool
    relaunches it beside itself. A dead owner is running nothing, so the
    exemption does not apply to it."""
    node = StepNode(id="a", step_type="tool", tool_name="write",
                    timeout_seconds=0, transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name="t", begin="a", steps=[node]))
    run_id = sf.create_run("t")
    sf.start_run(run_id)
    row_id = _insert_claim(
        sf, run_id, "a",
        f"tool-inline host=testhost pid={dead_pid} boot=testboot")

    assert run_id in sf.recover_stale_claims(stale_threshold_seconds=3600)
    assert _status(sf, row_id) == "pending"


def test_a_recycled_pid_is_not_mistaken_for_the_original(fake_self):
    """The container-restart case: pid 1 exists again and is a different
    process. The pid alone would say "alive"; the start marker says otherwise.

    Skipped where /proc is unavailable — there is no start marker to compare,
    and the module degrades to the lease by design.
    """
    if identity._pid_starttime(os.getpid()) is None:
        pytest.skip("no /proc: process start times are not observable here")
    stale = f"worker host=testhost pid={os.getpid()} boot=testboot start=1"
    assert owner_is_dead(stale) is True

    live = (f"worker host=testhost pid={os.getpid()} boot=testboot "
            f"start={identity._pid_starttime(os.getpid())}")
    assert owner_is_dead(live) is False


def test_a_live_owner_is_never_reclaimed_however_long_it_is_quiet(
        sf: SkillFlow, fake_self):
    """The defect the whole mechanism exists to remove.

    An agent step spends minutes inside a single LLM call and traces nothing
    while it waits, so the activity clock cannot tell "working" from "gone" —
    and the lease reaped the working one. Measured on a live run: 8 reclaims
    against 13 implementation steps, each surviving executor then failing its
    confirm on `version mismatch` and recording as failed work that had in fact
    been promoted. Liveness is a fact about a process, so ask the process.
    """
    run_id = _one_step_run(sf)
    mine = worker_identity("worker")
    row_id = _insert_claim(sf, run_id, "a", mine, age_seconds=6 * 3600)

    assert owner_is_dead(mine) is False
    # Six hours quiet against a three-minute lease, and still not reclaimed.
    assert sf.recover_stale_claims(stale_threshold_seconds=180) == []
    assert _status(sf, row_id) == "claimed"
    # Not even "everything is stale" reaches an owner that is demonstrably
    # running: there is no elapsed time that makes a live process dead.
    assert sf.recover_stale_claims(stale_threshold_seconds=-1) == []
    assert _status(sf, row_id) == "claimed"


def test_an_unknown_host_falls_back_to_the_lease(sf: SkillFlow, fake_self):
    """A claim from another kernel boot cannot be probed — its pid numbers mean
    nothing here — so it keeps exactly the recovery it had before."""
    run_id = _one_step_run(sf)
    foreign = "worker host=elsewhere pid=999999 boot=someotherboot"
    assert owner_is_dead(foreign) is None

    row_id = _insert_claim(sf, run_id, "a", foreign, age_seconds=5)
    assert sf.recover_stale_claims(stale_threshold_seconds=3600) == []
    assert _status(sf, row_id) == "claimed"

    assert run_id in sf.recover_stale_claims(stale_threshold_seconds=-1)
    assert _status(sf, row_id) == "pending"


def test_a_quiet_live_owner_and_a_dead_one_are_told_apart_in_one_sweep(
        sf: SkillFlow, fake_self, dead_pid):
    """Both rules at once, which is the only way they are ever exercised: one
    sweep over a table holding a working step and an abandoned one must move
    exactly one of them."""
    run_id = _one_step_run(sf)
    alive = _insert_claim(sf, run_id, "a", worker_identity("worker"),
                          age_seconds=9999)
    dead = _insert_claim(
        sf, run_id, "b",
        f"worker host=testhost pid={dead_pid} boot=testboot", age_seconds=1)

    assert run_id in sf.recover_stale_claims(stale_threshold_seconds=180)
    assert _status(sf, alive) == "claimed"
    assert _status(sf, dead) == "pending"


def test_a_legacy_claim_is_still_reaped_by_the_lease(sf: SkillFlow, fake_self):
    """The migration must not strand rows claimed by the old literal."""
    run_id = _one_step_run(sf)
    row_id = _insert_claim(sf, run_id, "a", "worker", age_seconds=99999)
    assert run_id in sf.recover_stale_claims(stale_threshold_seconds=60)
    assert _status(sf, row_id) == "pending"


# ── Change 2: the fence ─────────────────────────────────────────────

def test_every_claim_bumps_the_epoch(sf: SkillFlow, crash_the_owner):
    run_id = _one_step_run(sf)
    first = sf.claim_next_step(run_id)
    assert first.token.claim_epoch == 1

    crash_the_owner(sf, run_id)
    second = sf.claim_next_step(run_id)
    assert second.token.step_instance_id == first.token.step_instance_id
    assert second.token.claim_epoch == 2


def test_a_stale_confirm_is_refused_and_the_current_holder_succeeds(
        sf: SkillFlow, crash_the_owner):
    run_id = _one_step_run(sf)
    zombie = sf.claim_next_step(run_id).token

    crash_the_owner(sf, run_id)          # the claim is reset under its executor
    holder = sf.claim_next_step(run_id).token

    with pytest.raises(StaleClaimFenced):
        sf.confirm_step(zombie, StepResult(outputs={"who": "zombie"}))

    sf.confirm_step(holder, StepResult(outputs={"who": "holder"}))
    row = sf._conn.execute(
        "SELECT status, outputs_json FROM skillflow_steps WHERE id = ?",
        (holder.step_instance_id,)).fetchone()
    assert row["status"] == "completed"
    assert "holder" in row["outputs_json"]


def test_the_fence_fires_before_the_lifecycle_hooks(sf: SkillFlow, monkeypatch,
                                                    crash_the_owner):
    """The point of the whole change: on_deliver runs repo_apply, which makes
    real git commits. The version guard at the bottom of confirm_step let those
    land first and complained afterwards."""
    run_id = _one_step_run(sf)
    zombie = sf.claim_next_step(run_id).token
    crash_the_owner(sf, run_id)
    sf.claim_next_step(run_id)

    ran = []
    monkeypatch.setattr(SkillFlow, "_execute_lifecycle_hook",
                        lambda self, *a, **kw: ran.append(a) or {"passed": True})
    with pytest.raises(StaleClaimFenced):
        sf.confirm_step(zombie, StepResult(outputs={}))
    assert ran == [], "a fenced-out executor reached its lifecycle hooks"


def test_a_stale_fail_step_is_refused(sf: SkillFlow, crash_the_owner):
    """A zombie must not spend the replacement's retry budget or knock a live
    claim back to pending."""
    run_id = _one_step_run(sf)
    zombie = sf.claim_next_step(run_id).token
    crash_the_owner(sf, run_id)
    holder = sf.claim_next_step(run_id).token

    with pytest.raises(StaleClaimFenced):
        sf.fail_step(zombie, "zombie says it failed")
    assert _status(sf, holder.step_instance_id) == "claimed"


def test_a_stale_tool_call_is_refused(sf: SkillFlow, crash_the_owner):
    run_id = _one_step_run(sf)
    zombie = sf.claim_next_step(run_id).token
    crash_the_owner(sf, run_id)
    holder = sf.claim_next_step(run_id).token

    refused = sf.execute_tool(
        "read_file", {"path": "nope.txt"}, run_id=run_id, step_id="a",
        step_instance_id=zombie.step_instance_id,
        claim_epoch=zombie.claim_epoch)
    assert "reclaimed" in refused.get("error", "")

    allowed = sf.execute_tool(
        "read_file", {"path": "nope.txt"}, run_id=run_id, step_id="a",
        step_instance_id=holder.step_instance_id,
        claim_epoch=holder.claim_epoch)
    assert "reclaimed" not in str(allowed.get("error", ""))


def test_an_unfenced_caller_is_unaffected(sf: SkillFlow, crash_the_owner):
    """claim_epoch=0 means "no token offered" — the behaviour every host has
    today, and what a hand-built ClaimToken carries."""
    run_id = _one_step_run(sf)
    token = sf.claim_next_step(run_id).token
    crash_the_owner(sf, run_id)
    sf.claim_next_step(run_id)

    res = sf.execute_tool("read_file", {"path": "nope.txt"}, run_id=run_id,
                          step_id="a", step_instance_id=token.step_instance_id)
    assert "reclaimed" not in str(res.get("error", ""))


# ── Migration ───────────────────────────────────────────────────────

def test_an_old_database_gains_the_column_without_losing_its_claims(tmp_path):
    """A DB written before claim_epoch existed: the column is added, existing
    rows read 0, and 0 is unfenced on both sides — a claim that was in flight
    across the upgrade is never rejected by it."""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE skillflow_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, step_id TEXT NOT NULL,
            step_config_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            version INTEGER NOT NULL DEFAULT 1,
            retry_count INTEGER NOT NULL DEFAULT 0,
            validation_retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 3,
            inputs_json TEXT NOT NULL DEFAULT '{}',
            outputs_json TEXT NOT NULL DEFAULT '{}',
            result_flags_json TEXT NOT NULL DEFAULT '{}',
            last_error TEXT, claimed_at TEXT, claimed_by TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
    conn.execute(
        "INSERT INTO skillflow_steps (run_id, step_id, status, claimed_by) "
        "VALUES ('r1', 'a', 'claimed', 'worker')")
    conn.commit()
    conn.close()

    sf = SkillFlow(db)
    row = sf._conn.execute(
        "SELECT claim_epoch, claimed_by FROM skillflow_steps "
        "WHERE run_id = 'r1'").fetchone()
    assert row["claim_epoch"] == 0
    assert row["claimed_by"] == "worker"
    # 0 on the row means unfenced, whatever the caller carries.
    assert sf._epoch_holds(1, 7) is True
