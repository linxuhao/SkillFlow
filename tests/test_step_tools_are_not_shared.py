"""A step's read tools belong to that step, not to whoever claimed last.

`read`/`search`/`list` close over the source map of the step that built them —
its workspace, its staging dir, its code repo. They used to be registered in the
ToolLoader under those three fixed names, in one process-wide slot each, and
resolved by name at call time. With one project advancing at a time that was
safe only by accident: no second claim could land between a claim and its
execution. Once a host advances several projects concurrently the accident is
gone, and the last claim's closures answer every in-flight step.

Observed 2026-08-28 in AItelier: a code-review step in a repo-less project
listed an unrelated project's git repository (character art from a game the
reviewer then wrote findings about), because that project claimed a step while
the reviewer was still running.
"""

from dataclasses import replace

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import (
    PipelineGraph, StepNode, Transition, EndConditions, EndCondition,
)
from skillflow.tool_loader import ToolLoader


def _graph(name: str) -> PipelineGraph:
    return PipelineGraph(
        name=name, begin="review",
        steps=[
            StepNode(id="review", step_type="agent", agent_config="reviewer",
                     context=[{"source": {"step": "prior"}}],
                     transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=EndConditions(combinator="or", conditions=[
            EndCondition(type="node_reached", node="done", result="completed")]))


@pytest.fixture
def sf(tmp_path):
    # The REAL ToolLoader, not a mock. The whole defect lives in how a callable
    # is stored and resolved there, so a double that lacks that machinery would
    # make this suite pass (or fail) for reasons unrelated to the bug.
    engine = SkillFlow(":memory:", tool_loader=ToolLoader(),
                       workspace_base=str(tmp_path / "workspaces"),
                       projects_base=str(tmp_path / "projects"))
    engine.register_agent_config("reviewer", tools=["read_file"])
    return engine


def _repo(tmp_path, project_id: str, marker: str) -> None:
    """A code repo for *project_id* holding one identifying file."""
    d = tmp_path / "projects" / project_id
    d.mkdir(parents=True, exist_ok=True)
    (d / marker).write_text("x", encoding="utf-8")


def _claim(sf, graph_name: str, project_id: str):
    run_id = sf.create_run(graph_name, project_id=project_id)
    sf.start_run(run_id)
    sf.advance_run(run_id)
    return run_id, sf.claim_next_step(run_id)


def _listing(sf, run_id: str) -> str:
    out = sf.execute_tool("list", {}, run_id=run_id, step_id="review")
    return str(out)


def test_a_second_projects_claim_does_not_take_over_the_first_ones_reads(
        sf, tmp_path):
    """The bug, exactly: A claims, B claims, A reads — and must see A's repo."""
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    _repo(tmp_path, "proj_b", "ONLY_IN_B.txt")
    sf.register_graph(_graph("ga"))
    sf.register_graph(_graph("gb"))

    run_a, claimed_a = _claim(sf, "ga", "proj_a")
    assert "list" in claimed_a.inputs.get("_tool_schemas", {}), \
        "the step must actually be granted the read surface, or this proves nothing"

    # B claims while A is still executing — the moment that used to overwrite
    # A's closures.
    run_b, _ = _claim(sf, "gb", "proj_b")

    listing_a = _listing(sf, run_a)

    assert "ONLY_IN_A.txt" in listing_a
    assert "ONLY_IN_B.txt" not in listing_a, \
        "A's step read B's repository — the read tools are shared again"


def test_each_project_keeps_reading_its_own_repo_after_the_other_claims(
        sf, tmp_path):
    """Both directions: B must not inherit A's either."""
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    _repo(tmp_path, "proj_b", "ONLY_IN_B.txt")
    sf.register_graph(_graph("ga"))
    sf.register_graph(_graph("gb"))

    run_a, _ = _claim(sf, "ga", "proj_a")
    run_b, _ = _claim(sf, "gb", "proj_b")

    assert "ONLY_IN_B.txt" in _listing(sf, run_b)
    assert "ONLY_IN_A.txt" not in _listing(sf, run_b)
    # …and A still works after B's claim, i.e. the fix isn't "last claim loses".
    assert "ONLY_IN_A.txt" in _listing(sf, run_a)


def test_the_tools_are_released_when_the_step_ends(sf, tmp_path):
    """Reclaimed at step end: nothing may call a finished step's read surface.

    Without this the map grows for the life of the process and a late call from
    an abandoned executor still reaches a live source map.
    """
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, claimed_a = _claim(sf, "ga", "proj_a")
    assert "ONLY_IN_A.txt" in _listing(sf, run_a)

    sf.confirm_step(claimed_a.token, StepResult(outputs={}, flags={}))

    out = sf.execute_tool("list", {}, run_id=run_a, step_id="review")
    assert "ONLY_IN_A.txt" not in str(out)
    # A legible refusal, not "tool not found in any tools directory": the tool
    # exists, this step just no longer owns one.
    assert "per step" in out.get("error", "")


def test_a_failed_step_also_gives_its_tools_back(sf, tmp_path):
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, claimed_a = _claim(sf, "ga", "proj_a")
    sf.fail_step(claimed_a.token, "boom")
    assert "ONLY_IN_A.txt" not in _listing(sf, run_a)


def test_the_read_trio_is_never_left_in_the_shared_loader(sf, tmp_path):
    """The structural claim, not just the observable one.

    A test that only compares listings would still pass if the closures went
    back into a shared slot under different names. Nothing per-step may be
    reachable through the loader at all — only the NAME is global, so that
    is_native/is_dynamic keep classifying these tools correctly.
    """
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    _claim(sf, "ga", "proj_a")

    for name in ("read", "search", "list"):
        assert name not in sf._tool_loader._cache, \
            f"{name!r} put a callable in the shared loader"
        # …but the NAME is still known, so is_native keeps classifying it the
        # way it did when the callable lived there (runner mode branches on it).
        assert sf._tool_loader.is_native(name)
    assert sf._step_scoped_names >= {"read", "search", "list"}


# ── A claim owns its slot even when it binds nothing ──────────────────────

@pytest.mark.parametrize("break_it", ["raise", "no_read_fns"])
def test_a_claim_that_binds_no_read_tools_is_not_served_the_last_claims(
        sf, tmp_path, monkeypatch, break_it):
    """The bind sits under three conditions; on every skip the slot must be EMPTY.

    `_set_step_tools` is reached only when the step has context specs, the
    read-tool build did not raise, and it produced at least one callable. On any
    of those three skip paths the PREVIOUS claim's entry used to stay in the slot
    and answer this claim's reads — and entries only leave at confirm/fail, so a
    claim ended by the reaper, a timeout or a lifecycle failure leaves one behind.

    For a loop body that is not a cosmetic leak: the stale closures'
    'self'/'promoted' layer is the previous ITEM's directory, so item B reads
    item A's output as its own and produces silently wrong work — with no error
    anywhere, because a read that returns the wrong file returns successfully.
    """
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, first = _claim(sf, "ga", "proj_a")
    assert "ONLY_IN_A.txt" in _listing(sf, run_a), "no read surface to inherit"

    # Instance 1 ends WITHOUT confirm/fail — reaped, timed out, or dropped.
    with sf._tx() as conn:
        conn.execute(
            "UPDATE skillflow_steps SET status = 'completed' WHERE id = ?",
            (first.token.step_instance_id,))

    # Instance 2's read-tool build fails, each of the two silent ways.
    if break_it == "raise":
        def _boom(*a, **k):
            raise RuntimeError("wiring bug")
        monkeypatch.setattr("skillflow.read_tools.build_source_map", _boom)
    else:
        monkeypatch.setattr("skillflow.read_tools.make_read_tool_fns",
                            lambda *a, **k: {})

    second = sf.claim_next_step(run_a)
    assert second is not None, "the step did not re-enter; this proves nothing"

    out = sf.execute_tool("list", {}, run_id=run_a, step_id="review")
    assert "ONLY_IN_A.txt" not in str(out), \
        "the new claim was served the previous claim's closures"


# ── Release is a compare-and-delete on the STEP INSTANCE ──────────────────

def _reenter(sf, run_id: str, step_id: str):
    """End the current instance of *step_id* the way a confirm does, then claim
    the next one — the loop-body / Green-Red re-run shape."""
    with sf._tx() as conn:
        conn.execute(
            "UPDATE skillflow_steps SET status = 'completed' "
            "WHERE run_id = ? AND step_id = ? AND status = 'claimed'",
            (run_id, step_id))
    return sf.claim_next_step(run_id)


def test_a_late_release_cannot_blind_the_next_instance_of_the_same_step(
        sf, tmp_path):
    """The collision the release guard exists for, with the real values.

    A step that runs more than once in a run gets a FRESH `skillflow_steps` row
    per entry, and that INSERT does not list `claim_epoch` — it defaults to 0 and
    the first claim makes it 1. So instance N and instance N+1 both carry
    `claim_epoch == 1` and an epoch comparison cannot tell them apart, in exactly
    the loop-body / Green-Red-rerun case the guard was written for.

    `_assert_epoch` does not cover it either: it re-reads the ZOMBIE's OWN row,
    which nothing resets, so a token for a finished instance passes forever.

    Sequence: worker A finishes instance 1, the step re-enters as instance 2 and
    worker B is live; A's watchdog then fires a late `fail_step`. Compared on the
    epoch, that release deletes B's entry and B reads nothing for the rest of its
    step while believing it read everything.
    """
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, first = _claim(sf, "ga", "proj_a")
    second = _reenter(sf, run_a, "review")
    assert second is not None, "the step did not re-enter; this proves nothing"

    a, b = first.token, second.token
    assert a.step_instance_id != b.step_instance_id, \
        "not two instances; the test is not exercising the collision"
    assert a.claim_epoch == b.claim_epoch == 1, \
        "the epochs no longer collide — re-derive what identifies a claim"

    # Worker A's watchdog, one instance too late. `_assert_epoch` lets it in.
    sf.fail_step(a, "worker A's watchdog fired after the step moved on")

    assert "ONLY_IN_A.txt" in _listing(sf, run_a), \
        "a finished instance's release took the live instance's read surface"


def test_an_unfenced_token_cannot_blind_a_later_claim_of_the_SAME_row(
        sf, tmp_path):
    """The other collision: one ROW, two claims.

    Six sites reset a row to 'pending' (validation failure, lifecycle retry,
    fail-retry, checkpoint reject, the reaper…) and `claim_next_step` re-claims
    that SAME row, bumping only `claim_epoch`. So a row id is shared by every
    re-claim of it and cannot name a claim either.

    Reached here through the one shape `_assert_epoch` lets past with a
    superseded claim: `claim_epoch == 0`, which `_epoch_holds` deliberately
    admits ("a hand-built token, or a row that predates the column") — i.e. a
    host that does not forward epochs. With a forwarded epoch the fence refuses
    the call before release, which is exactly why this test builds the unfenced
    token instead of pretending the fenced path is broken.
    """
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, first = _claim(sf, "ga", "proj_a")

    # The row goes back to pending and is re-claimed — same row, next epoch.
    with sf._tx() as conn:
        conn.execute(
            "UPDATE skillflow_steps SET status = 'pending', "
            "version = version + 1 WHERE id = ?",
            (first.token.step_instance_id,))
    second = sf.claim_next_step(run_a)
    assert second is not None
    assert second.token.step_instance_id == first.token.step_instance_id, \
        "not a re-claim of one row; the test is not exercising the collision"
    assert second.token.claim_epoch == first.token.claim_epoch + 1

    # The superseded worker confirms with an UNFENCED token: same row, epoch 0.
    stale = replace(first.token, claim_epoch=0)
    sf._assert_epoch(stale, "probe")          # documents that the fence admits it
    sf.fail_step(stale, "a host that does not forward claim epochs")

    assert "ONLY_IN_A.txt" in _listing(sf, run_a), \
        "an unfenced token released the live re-claim's read surface"


def test_the_owning_instance_can_still_release_its_own_tools(sf, tmp_path):
    """The guard must not become a refusal to release at all."""
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, claimed = _claim(sf, "ga", "proj_a")

    sf._release_step_tools(run_a, "review", claimed.token.step_instance_id,
                           claimed.token.claim_epoch)
    assert "ONLY_IN_A.txt" not in _listing(sf, run_a)


def test_a_token_with_half_an_identity_releases_nothing(sf, tmp_path):
    """A hand-built token — the shape `_epoch_holds` deliberately admits — cannot
    say which claim it is, with EITHER half missing. The two mistakes are not
    symmetric: releasing the wrong entry blinds a running step, keeping one costs
    a dict entry that the next claim of that step clears and the cap bounds. So
    it keeps it.
    """
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, claimed = _claim(sf, "ga", "proj_a")
    inst = claimed.token.step_instance_id
    epoch = claimed.token.claim_epoch

    sf._release_step_tools(run_a, "review", 0, epoch)
    sf._release_step_tools(run_a, "review", None, epoch)
    sf._release_step_tools(run_a, "review", inst, 0)
    sf._release_step_tools(run_a, "review", inst, None)

    assert "ONLY_IN_A.txt" in _listing(sf, run_a), \
        "an unidentifiable token released a live claim's tools"


# ── The loader keeps only NAMES, and only the right ones ──────────────────

def test_adding_a_tools_dir_forgets_registered_callables_but_not_step_names(
        tmp_path):
    """`add_tools_dir` clears `_cache`, which is where a registered dynamic
    tool's callable lives — so that name must stop counting as native, exactly
    as before. A step-owned name has no callable in this loader to invalidate,
    so it must survive: runner mode branches on `is_native` to decide between
    executing a tool and delegating it to the agent.
    """
    from skillflow.tool_loader import ToolLoader
    loader = ToolLoader(tmp_path / "native")
    (tmp_path / "native").mkdir()

    loader.register_dynamic_tool("registered", {"name": "registered"},
                                 lambda **kw: {})
    loader.declare_dynamic("list")
    assert loader.is_native("registered") and loader.is_native("list")

    loader.add_tools_dir(tmp_path / "extra")

    assert not loader.is_native("registered"), \
        "its callable was just discarded; it must not still read as native"
    assert loader.is_native("list"), \
        "a step-owned name has nothing in this loader to invalidate"


# ── Bounding must not blind a live step ───────────────────────────────────


def _claim_n(sf, tmp_path, n: int) -> list:
    """*n* independent runs, each with a live claim and its own read surface."""
    out = []
    for i in range(n):
        pid = f"proj_{i}"
        _repo(tmp_path, pid, f"ONLY_IN_{i}.txt")
        gname = f"g{i}"
        sf.register_graph(_graph(gname))
        out.append(_claim(sf, gname, pid))
    return out


def test_the_cap_never_evicts_an_entry_whose_claim_is_still_live(
        sf, tmp_path, monkeypatch):
    """The invariant the evictor has to keep, stated where it can be checked.

    Every entry here belongs to a claim that is still `status='claimed'` at its
    recorded epoch. None of them may go, however far over the cap the map is —
    a evicted entry means a step that is still working reads nothing for the
    rest of its step while believing it read everything.
    """
    monkeypatch.setattr(type(sf), "_STEP_TOOL_CAP", 1)
    claims = _claim_n(sf, tmp_path, 3)
    assert len(sf._step_tools) == 3, "the fixture did not build three claims"

    sf._evict_ended_step_tools()

    assert len(sf._step_tools) == 3, \
        "a live claim lost its read surface to the cap"
    for i, (run_id, _c) in enumerate(claims):
        assert f"ONLY_IN_{i}.txt" in _listing(sf, run_id)


def test_a_step_silent_inside_one_long_generation_turn_keeps_its_tools(
        sf, tmp_path, monkeypatch):
    """The specific shape an idle clock gets wrong.

    An earlier evictor dropped entries idle longer than the reap threshold and
    said that matched `recover_stale_claims`. It does not: that reaper decides on
    OWNERSHIP first — `owner_is_dead is False` skips the row before any clock is
    read — so a claim whose process the OS reports alive is never reaped however
    long it has been silent. An agent inside one long generation turn traces
    nothing, so neither its lease clock nor a read-tool clock moves WHILE IT
    WORKS. An idle-clock evictor therefore blinds exactly the step the reaper
    protects.

    Here nothing has touched the entry since the claim, and the reaper — asked
    with a threshold of zero, which makes every claim maximally overdue —
    still refuses to reap it. So must the evictor.
    """
    monkeypatch.setattr(type(sf), "_STEP_TOOL_CAP", 0)
    (run_id, _c), = _claim_n(sf, tmp_path, 1)

    assert sf.recover_stale_claims(stale_threshold_seconds=0) == [], \
        "the reaper reclaimed this claim; it is not the protected shape"

    sf._evict_ended_step_tools()

    assert "ONLY_IN_0.txt" in _listing(sf, run_id), \
        "a live-but-silent step lost its read surface"


def test_the_cap_drops_an_entry_whose_claim_has_ENDED(sf, tmp_path,
                                                      monkeypatch):
    """…and it must actually drop something, or it is not a bound at all.

    A row that is no longer 'claimed' is a claim that is over, whatever left it
    that way.
    """
    monkeypatch.setattr(type(sf), "_STEP_TOOL_CAP", 1)
    claims = _claim_n(sf, tmp_path, 2)
    (run0, c0), (run1, _c1) = claims
    with sf._tx() as conn:
        conn.execute("UPDATE skillflow_steps SET status = 'completed' "
                     "WHERE id = ?", (c0.token.step_instance_id,))

    sf._evict_ended_step_tools()

    assert (run0, "review") not in sf._step_tools, \
        "an ended claim's closures were kept"
    assert (run1, "review") in sf._step_tools, \
        "the live claim was evicted alongside the ended one"


def test_the_cap_drops_an_entry_superseded_by_a_later_claim_of_one_row(
        sf, tmp_path, monkeypatch):
    """Same row, still 'claimed', but at a LATER epoch — the entry belongs to a
    claim that has been replaced, so it is over too."""
    monkeypatch.setattr(type(sf), "_STEP_TOOL_CAP", 0)
    (run0, c0), = _claim_n(sf, tmp_path, 1)
    key = (run0, "review")
    inst, epoch, fns = sf._step_tools[key]
    # Stamp the entry with the PREVIOUS epoch, as a superseded claim's would be,
    # while the row stays claimed at the current one.
    sf._step_tools[key] = (inst, epoch - 1, fns)

    sf._evict_ended_step_tools()

    assert key not in sf._step_tools, \
        "a superseded claim's closures survived; the next lookup gets them"


def test_the_cap_spares_the_entry_the_lease_reaper_deliberately_spared(
        sf, tmp_path, monkeypatch):
    """The evictor and the reaper must agree about a lease-reset row.

    `recover_stale_claims`' lease branch resets a possibly-still-alive worker's
    row to 'pending' WITHOUT bumping `claim_epoch`, and leaves the entry alone on
    purpose (`test_a_lease_reaped_claim_keeps_its_read_surface`): the worker's
    epoch still satisfies `_epoch_holds`, so nothing fences its tool calls and
    they must still be served.

    An evictor that reads "ended" as `status != 'claimed'` deletes exactly that
    entry — the two halves of this file then contradict each other, and the
    worker the reaper protected reads nothing for the rest of its step while
    believing it read everything.
    """
    from skillflow.identity import owner_is_dead

    unknown = "worker host=gone pid=999999"
    assert owner_is_dead(unknown) is None, \
        "this identity is meant to be undeterminable; the branch under test moved"

    monkeypatch.setattr(type(sf), "_STEP_TOOL_CAP", 0)
    (run0, claimed), = _claim_n(sf, tmp_path, 1)

    with sf._tx() as conn:
        conn.execute(
            "UPDATE skillflow_steps SET claimed_by = ?, claimed_at = ?, "
            "updated_at = ? WHERE run_id = ? AND step_id = ?",
            (unknown, "2000-01-01 00:00:00", "2000-01-01 00:00:00",
             run0, "review"))
    assert sf.recover_stale_claims(stale_threshold_seconds=1), \
        "the reaper did not reclaim; this test would prove nothing"

    with sf._tx() as conn:
        row = conn.execute(
            "SELECT status, claim_epoch FROM skillflow_steps WHERE run_id = ? "
            "AND step_id = ?", (run0, "review")).fetchone()
    assert row["status"] == "pending", "the reaper no longer resets to pending"
    assert (row["claim_epoch"] or 0) == claimed.token.claim_epoch, \
        "the reaper now bumps the epoch; the unfenced-worker premise moved"

    sf._evict_ended_step_tools()

    assert "ONLY_IN_0.txt" in _listing(sf, run0), \
        "the evictor dropped the entry the reaper deliberately spared"


def test_an_unreadable_step_table_evicts_nothing(sf, tmp_path, monkeypatch):
    """Best-effort: not knowing which claims ended is not a licence to guess.
    A few hundred leaked closures cost memory; a wrong eviction costs a running
    step its eyes."""
    monkeypatch.setattr(type(sf), "_STEP_TOOL_CAP", 0)
    (run0, _c0), = _claim_n(sf, tmp_path, 1)

    def _boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(sf, "_ro", _boom)

    sf._evict_ended_step_tools()

    assert (run0, "review") in sf._step_tools


def test_the_cap_is_enforced_through_a_REAL_claim_not_only_when_called_directly(
        sf, tmp_path, monkeypatch):
    """Every other test here calls `_evict_ended_step_tools()` directly.

    Its only real caller is `_set_step_tools`, reached from inside
    `claim_next_step` — where the connection state is not the same as in a bare
    call, and where the evictor's best-effort `except` would turn any DB problem
    into "evicted nothing", silently and forever. This one therefore goes
    through a claim, so the cap is pinned on the path that actually runs it.

    (It does NOT discriminate between `_ro` and `_tx` today: a nested `_tx`
    there happens to succeed, because something earlier in `claim_next_step`
    commits the connection. `_ro` is used because it is correct regardless of
    where that commit falls — see the comment on the read.)
    """
    monkeypatch.setattr(type(sf), "_STEP_TOOL_CAP", 1)
    (run0, c0), = _claim_n(sf, tmp_path, 1)
    with sf._tx() as conn:                       # instance 1 ends, unreleased
        conn.execute("UPDATE skillflow_steps SET status = 'completed' "
                     "WHERE id = ?", (c0.token.step_instance_id,))

    _repo(tmp_path, "proj_1", "ONLY_IN_1.txt")
    sf.register_graph(_graph("g1"))
    _claim(sf, "g1", "proj_1")                   # this claim trips the cap

    assert (run0, "review") not in sf._step_tools, \
        "the cap never fired: the evictor cannot read the step table from " \
        "inside the claim transaction"


def test_the_over_cap_warning_is_not_emitted_under_the_map_lock(sf, tmp_path):
    """`_step_tools_lock` is documented as held across dict operations only, and
    it is taken on the hot path of every read-tool call. Logging is I/O behind a
    handler lock of its own; emitting under this one couples one step's read call
    to another's log flush — and makes the documented invariant false.
    """
    import logging as _logging

    under_lock = []

    class _Probe(_logging.Handler):
        def emit(self, record):
            got = sf._step_tools_lock.acquire(blocking=False)
            under_lock.append(not got)
            if got:
                sf._step_tools_lock.release()

    probe = _Probe()
    logger = _logging.getLogger("skillflow")
    logger.addHandler(probe)
    try:
        object.__setattr__(sf, "_STEP_TOOL_CAP", 0)
        _claim_n(sf, tmp_path, 1)      # claiming trips the cap → warning
    finally:
        logger.removeHandler(probe)

    assert under_lock, "no warning was emitted; this test would prove nothing"
    assert not any(under_lock), "the over-cap warning was logged under the lock"


def test_the_reaper_never_takes_a_step_its_read_surface(sf, tmp_path):
    """The reaper must not touch `_step_tools`, on EITHER branch.

    It used to drop the entry when `owner_is_dead` said True, justified by "the
    owner process is dead, so nothing is left to call these closures". That
    reason does not hold. `_step_tools` is in-memory and per SkillFlow instance,
    so an entry exists only for a claim THIS process made — while the reaper
    scans every claimed row in a shared DB, including rows other processes own.
    A claim whose owner the OS reports GONE was therefore made by some other
    process; ours is demonstrably alive, since it is running the reaper. The
    only entry a keyless pop could reach is one belonging to a claim of ours,
    and taking that is precisely the silent degrade this area exists to prevent.

    The identity below re-stamps OUR OWN live claim as dead (this process's boot
    id and pid, with a `start=` that cannot match /proc — the recycled-pid case),
    which is the one construction that puts a live entry under a dead-owner row.
    """
    import os
    from skillflow.identity import _self_identity, owner_is_dead

    me = _self_identity()
    dead = (f"worker host={me['host']} pid={os.getpid()} "
            f"boot={me.get('boot')} start=1")
    if owner_is_dead(dead) is not True:
        pytest.skip("owner liveness is not observable on this platform")

    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, _ = _claim(sf, "ga", "proj_a")
    assert (run_a, "review") in sf._step_tools

    with sf._tx() as conn:
        conn.execute(
            "UPDATE skillflow_steps SET claimed_by = ? "
            "WHERE run_id = ? AND step_id = ?", (dead, run_a, "review"))

    reaped = sf.recover_stale_claims(stale_threshold_seconds=1)

    assert reaped, "the reaper did not reclaim; this test would prove nothing"
    assert "ONLY_IN_A.txt" in _listing(sf, run_a), \
        "the reaper stripped a step-owned read surface out of this process's map"


def test_a_lease_reaped_claim_keeps_its_read_surface(sf, tmp_path):
    """A lease reap is a GUESS, and dropping the tools makes a wrong one fatal.

    The reset UPDATE in `recover_stale_claims` writes status, version,
    claimed_at, claimed_by and inputs_json — not `claim_epoch`, which is bumped
    only in `claim_next_step` and `_claim_tool_step_in_tx`. So a worker the
    lease condemned while it was still alive keeps an epoch that satisfies
    `_epoch_holds`: `execute_tool` does not fence its calls, they arrive and
    find nothing, and every read/search/list for the rest of that step answers
    "this step has no read surface". The step then writes its output having read
    nothing — silently.

    `claimed_by` here carries a pid but no boot marker, so `owner_is_dead`
    returns None (not determinable) and the lease is the only thing condemning
    it — which is the whole point.
    """
    from skillflow.identity import owner_is_dead

    unknown = "worker host=gone pid=999999"
    assert owner_is_dead(unknown) is None, \
        "this identity is meant to be undeterminable; the branch under test moved"

    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, claimed = _claim(sf, "ga", "proj_a")

    with sf._tx() as conn:
        conn.execute(
            "UPDATE skillflow_steps SET claimed_by = ?, claimed_at = ?, "
            "updated_at = ? WHERE run_id = ? AND step_id = ?",
            (unknown, "2000-01-01 00:00:00", "2000-01-01 00:00:00",
             run_a, "review"))

    assert sf.recover_stale_claims(stale_threshold_seconds=1), \
        "the reaper did not reclaim; this test would prove nothing"

    # The epoch the maybe-live executor still holds is unchanged — so nothing
    # fences its calls, and they must still be served.
    with sf._tx() as conn:
        row = conn.execute(
            "SELECT claim_epoch FROM skillflow_steps WHERE run_id = ? AND "
            "step_id = ?", (run_a, "review")).fetchone()
    assert (row["claim_epoch"] or 0) == claimed.token.claim_epoch

    assert "ONLY_IN_A.txt" in _listing(sf, run_a), \
        "a lease-reaped (possibly live) step lost its read surface"


def test_a_tool_lookup_does_not_wait_on_an_engine_transaction(sf, tmp_path):
    """`_step_tool_fn` is on the hot path of every tool call. Sharing the engine
    RLock would make one project's tool call block on another project's claim or
    confirm — cross-project coupling re-introduced by the fix meant to remove it.
    """
    import threading
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, _ = _claim(sf, "ga", "proj_a")

    done = threading.Event()

    def _lookup():
        sf._step_tool_fn(run_a, "review", "list")
        done.set()

    with sf._lock:                       # as _tx does, for a whole transaction
        t = threading.Thread(target=_lookup, daemon=True)
        t.start()
        assert done.wait(timeout=2.0), \
            "tool lookup blocked on the engine lock held by another thread"


# ── the per-step refusal must not shadow a real on-disk tool ──────────────

def test_a_real_tool_directory_named_read_is_not_shadowed(sf, tmp_path):
    """`_step_scoped_names` only ever grows, so a bare membership test would
    refuse a genuine `read` tool for the rest of the process's life if one were
    ever added to a tools directory. `is_dynamic` re-checks the disk, so an
    on-disk tool wins.

    (There is no such tool today — the names are the unified trio. This pins the
    refusal to "nothing on disk answers to it" rather than to a name list.)
    """
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, claimed = _claim(sf, "ga", "proj_a")
    sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))
    # With no step surface, `read` is refused as step-scoped…
    assert "per step" in sf.execute_tool(
        "read", {}, run_id=run_a, step_id="review").get("error", "")

    # …until a real tool directory provides one.
    tools_dir = tmp_path / "tools" / "read"
    tools_dir.mkdir(parents=True)
    (tools_dir / "tool.yaml").write_text(
        "name: read\ndescription: an on-disk read tool\nentrypoint: impl.read\n"
        "parameters: {}\n", encoding="utf-8")
    (tools_dir / "impl.py").write_text(
        "def read(**kw):\n    return {'from_disk': True}\n", encoding="utf-8")
    sf._tool_loader.add_tools_dir(tools_dir.parent)

    out = sf.execute_tool("read", {}, run_id=run_a, step_id="review")
    assert out.get("from_disk") is True, \
        f"the step-scoped refusal shadowed a real on-disk tool: {out}"


def test_a_loop_node_is_refused_like_a_gate(sf, tmp_path):
    """A caller that reacts to `advance_run`'s silence by asking us to claim
    must not be handed a control node.

    `advance_run` leaves `current_node` on a loop whenever `_resolve_loop`
    returns None, and that branch fails nothing — the run sits there, still
    `running`. Claiming it hands the host's runner a step with no
    `agent_config`, which it can only fail. Refusing is what lets the caller's
    own "nothing claimable" branch report a reason instead.
    """
    from skillflow.graph import LoopConfig

    sf.register_graph(PipelineGraph(
        name="gl", begin="each",
        steps=[
            StepNode(id="each", step_type="loop",
                     loop=LoopConfig(source={"step": "seed", "file": "items.json"},
                                     item_as="item"),
                     transitions=[Transition(to="work")]),
            StepNode(id="work", step_type="agent", agent_config="reviewer",
                     transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=EndConditions(combinator="or", conditions=[
            EndCondition(type="node_reached", node="done", result="completed")])))
    run_id = sf.create_run("gl", project_id="proj_a")
    sf.start_run(run_id)

    with sf._tx() as conn:
        conn.execute("UPDATE skillflow_runs SET current_node='each' WHERE id=?",
                     (run_id,))

    assert sf.claim_next_step(run_id) is None, \
        "a loop node was claimed; the runner can only fail it"
