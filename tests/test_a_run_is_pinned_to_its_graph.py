"""Editing a graph must not change what an already-started run is executing.

A run stored only `graph_name`, and `_get_resolver_for_run` resolved that name
against whatever was registered *now*. So a config edit landing mid-run
retargeted the run's remaining steps — its finished steps had been validated
against one set of rules and its next ones against another — and an edit after a
run finished made its trace describe a graph that no longer existed anywhere.

`skillflow_graphs.version` did not help. It was bumped blindly on every
registration, and hosts re-register every config on every boot, so it counted
process restarts: a live AItelier deployment had every graph at version 312 with
identical `updated_at`, none of them edited 312 times.
"""

import pytest

from skillflow.core import SkillFlow, StepResult, graph_digest
from skillflow.graph import (
    PipelineGraph, StepNode, Transition, EndConditions, EndCondition,
)


def _graph(name: str, *, retries: int = 3) -> PipelineGraph:
    """A minimal valid graph whose only varying knob is step `a`'s retry budget —
    a real execution parameter, and one that leaves the graph valid either way."""
    return PipelineGraph(
        name=name, begin="a",
        steps=[
            StepNode(id="a", step_type="agent", agent_config="host",
                     max_retries=retries, transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=EndConditions(combinator="or", conditions=[
            EndCondition(type="node_reached", node="done", result="completed")]))


@pytest.fixture
def sf(tmp_path):
    sf = SkillFlow(str(tmp_path / "sf.db"), workspace_base=str(tmp_path),
                   projects_base=str(tmp_path), trace_enabled=False,
                   artifact_history=False)
    sf.agent_registry.register("host", model="host")
    return sf


def test_re_registering_identical_content_mints_no_version(sf):
    """The boot-scan case. Without it the number counts restarts, not edits."""
    first = sf.register_graph(_graph("g"))
    for _ in range(5):
        again = sf.register_graph(_graph("g"))

    assert first == 1
    assert again == 1, "identical content minted a new version"
    assert len(sf.list_graph_versions("g")) == 1


def test_changed_content_mints_a_version_and_keeps_the_old_one(sf):
    sf.register_graph(_graph("g"))
    v2 = sf.register_graph(_graph("g", retries=9))

    assert v2 == 2
    versions = sf.list_graph_versions("g")
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["digest"] != versions[1]["digest"]

    # The REPLACED content is still readable — that is the whole point.
    old = sf.get_graph_version("g", 1)
    # to_dict() omits a field at its default, so read it the way a caller would.
    assert old["graph"]["steps"][0].get("max_retries", 3) == 3
    assert sf.get_graph_version("g", 2)["graph"]["steps"][0]["max_retries"] == 9
    assert graph_digest(old["graph"]) == old["digest"]


def test_a_run_keeps_executing_the_graph_it_started_with(sf):
    """The defect. Step `a` had 3 retries when the run started; 9 after."""
    sf.register_graph(_graph("g"))
    run_id = sf.create_run("g", project_id="p1")
    sf.start_run(run_id)

    sf.register_graph(_graph("g", retries=9))

    node = sf._get_resolver_for_run(run_id).get_node("a")
    assert node.max_retries == 3, (
        "the run resolved against the edited graph — an edit landing mid-run "
        "retargeted a run already in flight")


def test_a_run_started_after_the_edit_gets_the_edit(sf):
    """The control. Without it, pinning to version 1 forever would also pass."""
    sf.register_graph(_graph("g"))
    sf.register_graph(_graph("g", retries=9))
    run_id = sf.create_run("g", project_id="p1")

    node = sf._get_resolver_for_run(run_id).get_node("a")
    assert node.max_retries == 9


def test_the_run_reports_which_version_it_is_pinned_to(sf):
    sf.register_graph(_graph("g"))
    run_id = sf.create_run("g", project_id="p1")
    assert sf.graph_version_for_run(run_id) == {
        "graph_name": "g", "version": 1,
        "digest": graph_digest(_graph("g").to_dict()),
        "latest_version": 1, "is_latest": True}

    sf.register_graph(_graph("g", retries=9))
    after = sf.graph_version_for_run(run_id)
    assert after["version"] == 1 and after["latest_version"] == 2
    assert after["is_latest"] is False


def test_a_legacy_run_without_a_pin_resolves_by_name(sf):
    """Rows written before this column existed must keep working, and must not
    claim a version they never had — `is_latest` is None, not True."""
    sf.register_graph(_graph("g"))
    run_id = sf.create_run("g", project_id="p1")
    with sf._tx() as conn:
        conn.execute("UPDATE skillflow_runs SET graph_version = NULL, "
                     "graph_digest = NULL WHERE id = ?", (run_id,))

    sf.register_graph(_graph("g", retries=9))

    assert sf._get_resolver_for_run(run_id).get_node("a").max_retries == 9
    assert sf.graph_version_for_run(run_id)["is_latest"] is None


def test_repinning_is_how_a_run_in_flight_adopts_an_edit(sf):
    """Pinning would otherwise remove a real recovery action — editing a graph
    to unstick a run already in flight. It stays available, but as an explicit
    call rather than a side effect of registering."""
    sf.register_graph(_graph("g"))
    run_id = sf.create_run("g", project_id="p1")
    sf.register_graph(_graph("g", retries=9))

    moved = sf.repin_run(run_id)

    assert (moved["from_version"], moved["to_version"]) == (1, 2)
    assert sf._get_resolver_for_run(run_id).get_node("a").max_retries == 9
    assert sf.graph_version_for_run(run_id)["is_latest"] is True


def test_repinning_to_a_version_that_does_not_exist_is_refused(sf):
    """Silently pinning to nothing would put the run back on the live graph —
    the exact behaviour this replaced, reachable through a typo."""
    sf.register_graph(_graph("g"))
    run_id = sf.create_run("g", project_id="p1")

    with pytest.raises(Exception, match="no version 7"):
        sf.repin_run(run_id, version=7)
    assert sf.graph_version_for_run(run_id)["version"] == 1


def test_the_pin_names_the_graph_the_step_rows_were_built_from(sf):
    """`register_graph` publishes to `_graphs` BEFORE its transaction, so "the
    latest version row" and "the graph create_run just used" can disagree — the
    in-memory graph survives a failed transaction. Pinning by digest keeps the
    run's rows and its resolver on one graph; pinning "latest" would not."""
    sf.register_graph(_graph("g"))                      # v1, committed
    # An edit whose transaction never landed: the in-memory graph is the new
    # one, the history still ends at v1.
    edited = _graph("g", retries=9)
    sf._graphs["g"] = edited
    sf._resolvers["g"] = __import__("skillflow.graph", fromlist=["GraphResolver"]
                                    ).GraphResolver(edited)

    run_id = sf.create_run("g", project_id="p1")

    assert sf.graph_version_for_run(run_id)["version"] is None, \
        "pinned v1 while building the run from the unversioned edited graph"
    # NULL resolves by name, which is the graph the rows came from.
    assert sf._get_resolver_for_run(run_id).get_node("a").max_retries == 9


def test_releasing_a_claim_does_not_spend_the_retry_budget(sf):
    """A cancelled driver is not a failed step. `fail_step(retryable=True)`
    increments retry_count, so three cancellations would kill a healthy step
    with an error blaming it for what the client did."""
    sf.register_graph(_graph("g"))
    run_id = sf.create_run("g", project_id="p1")
    sf.start_run(run_id)
    claimed = sf.claim_next_step(run_id)

    out = sf.release_claim(claimed.token, "driver cancelled")

    row = sf._conn.execute(
        "SELECT status, retry_count FROM skillflow_steps WHERE id = ?",
        (claimed.token.step_instance_id,)).fetchone()
    assert out["released"] is True
    assert row["status"] == "pending"
    assert row["retry_count"] == 0, "a cancellation spent a retry"
    assert sf.claim_next_step(run_id) is not None, "the step is claimable again"


def test_releases_are_counted_and_eventually_fail_the_step(sf):
    """A cause that recurs forever would otherwise re-run the step forever, at
    full LLM cost, saying nothing. Same 3-strike shape as the reaper's
    `_stale_recovery_count`."""
    sf.register_graph(_graph("g"))
    run_id = sf.create_run("g", project_id="p1")
    sf.start_run(run_id)

    outs = []
    for _ in range(3):
        c = sf.claim_next_step(run_id)
        if c is None:
            break
        outs.append(sf.release_claim(c.token, "driver cancelled"))

    assert [o["released"] for o in outs] == [True, True, False]
    assert outs[-1]["failed"] is True
    final = sf._conn.execute(
        "SELECT status, last_error FROM skillflow_steps WHERE run_id = ? "
        "AND step_id = 'a' ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    assert final["status"] == "failed"
    assert "released this claim" in final["last_error"]


def test_releasing_a_step_someone_else_already_resolved_is_a_no_op(sf):
    """The driver's cancellation can race its own replacement. Rewriting a
    completed step to `pending` would undo work that landed."""
    sf.register_graph(_graph("g"))
    run_id = sf.create_run("g", project_id="p1")
    sf.start_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult(outputs={}))

    out = sf.release_claim(claimed.token, "driver cancelled")

    assert out["released"] is False
    row = sf._conn.execute(
        "SELECT status FROM skillflow_steps WHERE id = ?",
        (claimed.token.step_instance_id,)).fetchone()
    assert row["status"] == "completed"
