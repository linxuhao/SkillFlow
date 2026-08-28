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


# ── Release is a compare-and-delete, not a blind pop ──────────────────────

def test_a_stale_executor_cannot_release_its_replacements_tools(sf, tmp_path):
    """`(run_id, step_id)` names the STEP, not the claim.

    `_assert_epoch` narrows the window in which a reclaimed executor can reach
    the release, but assert and release are separate operations — a stall
    between them lets the reaper hand the step to a replacement whose entry sits
    under the same key. A blind pop would delete it, and the replacement would
    run its whole step with no read surface: a silent degrade, which is the
    exact failure shape this area exists to stop producing.
    """
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, claimed = _claim(sf, "ga", "proj_a")
    live_epoch = claimed.token.claim_epoch

    # A stale claim generation tries to give the tools back.
    sf._release_step_tools(run_a, "review", live_epoch - 1)
    assert "ONLY_IN_A.txt" in _listing(sf, run_a), \
        "a stale epoch released the live claim's tools"

    # The owner's own release still works.
    sf._release_step_tools(run_a, "review", live_epoch)
    assert "ONLY_IN_A.txt" not in _listing(sf, run_a)


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

def test_the_cap_never_evicts_an_entry_that_could_still_be_running(sf, monkeypatch):
    """Insertion order would sacrifice the OLDEST entry — under load that is the
    longest-RUNNING claim, the one most likely to still need its tools. Only
    entries idle past the stale threshold are eligible; if none are, keep them
    all."""
    monkeypatch.setattr(type(sf), "_STEP_TOOL_CAP", 2)
    for i in range(4):
        sf._set_step_tools(f"run{i}", "s", {"list": lambda: None}, 1)

    assert len(sf._step_tools) == 4, \
        "young entries were evicted; a live step just lost its read surface"

    # Age the first one past the threshold, then trip the cap again.
    k = ("run0", "s")
    epoch, fns, _ = sf._step_tools[k]
    sf._step_tools[k] = (epoch, fns, 0.0)
    sf._set_step_tools("run4", "s", {"list": lambda: None}, 1)

    assert k not in sf._step_tools
    assert ("run4", "s") in sf._step_tools


def test_the_cap_measures_idleness_not_claim_age(sf, monkeypatch):
    """The guard has to read the same clock staleness is measured on.

    `recover_stale_claims` measures the ACTIVITY clock (`updated_at`,
    heartbeated). A step that keeps working keeps that clock fresh and is never
    reaped — while its CREATION stamp is by definition the oldest in the map. A
    guard filtering on creation age therefore evicts exactly the entry it exists
    to protect: the longest-running claim goes first.

    Here the oldest entry is the one still CALLING its read tools. It must be
    the last to go, not the first.
    """
    monkeypatch.setattr(type(sf), "_STEP_TOOL_CAP", 2)
    for i in range(3):
        sf._set_step_tools(f"run{i}", "s", {"list": lambda: None}, 1)
    # Everything is idle enough to be evictable…
    for k, (epoch, fns, _) in list(sf._step_tools.items()):
        sf._step_tools[k] = (epoch, fns, 0.0)

    # …but run0, the one created first, is still using its read surface.
    assert sf._step_tool_fn("run0", "s", "list") is not None

    sf._set_step_tools("run3", "s", {"list": lambda: None}, 1)

    assert ("run0", "s") in sf._step_tools, \
        "the entry that just called a read tool was evicted first"


def test_a_claim_whose_owner_is_gone_gives_its_tools_back(sf, tmp_path):
    """Where an abandoned claim is KNOWN dead, reclaiming the entry is free.

    The claim is re-stamped with an identity `owner_is_dead` reports True for:
    this process's own boot id and pid, with a `start=` marker that cannot match
    /proc — the recycled-pid case. Without a dead owner the reaper falls to the
    lease, which is a different branch (see the test below).
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
    assert (run_a, "review") not in sf._step_tools


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
