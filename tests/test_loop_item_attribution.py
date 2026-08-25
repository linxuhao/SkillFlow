"""A loop-body step instance records WHICH item it ran for.

`step_id` is not enough inside a loop: a fan-out over six items runs the body
six times, and retries plus review loop-backs push that higher, so a host asking
"what happened during this run" sees nine `t_impl` rows and no way to tell them
apart. Completion order cannot reconstruct it — that is exactly what retries and
loop-backs scramble — and the per-item OUTPUT dirs (`{step}/{item}/`) are
project-scoped and replaced in place, so they answer "what does the workspace
hold now", not "what did THIS run do".

So the item is stamped on the row at claim, read from the same
`skillflow_loop_state.current_item` that already drives per-item promotion and
read routing.
"""

import json
from pathlib import Path

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph

FIXTURES = Path(__file__).parent / "fixtures"


def _prepare(sf, execution_order, fixture="loop_step.yaml",
             project_id="loop-attrib"):
    """Load a loop fixture, run `prepare`, seed its manifest. Returns run_id."""
    sf.register_agent_config("echo_agent", model="mock", tools=[])
    graph = PipelineGraph.from_yaml(str(FIXTURES / fixture))
    sf.register_graph(graph)
    run_id = sf.create_run(graph.name, project_id=project_id)
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    assert claimed.step_id == "prepare"
    step_dir = sf._workspace.get_step_tmp_dir(
        project_id, sf._get_graph_name(run_id), "prepare")
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / "tasks_manifest.json").write_text(
        json.dumps({"execution_order": execution_order}))
    sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))
    return run_id


def _drive(sf, run_id, max_iter=200):
    """Run to a terminal state, confirming everything that gets claimed."""
    for _ in range(max_iter):
        sf.advance_run(run_id)
        if sf.get_run(run_id)["status"] in ("completed", "failed", "paused"):
            break
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            continue
        sf.confirm_step(claimed.token, StepResult())


def _rows(sf, run_id, step_id):
    return [s for s in sf.get_steps(run_id) if s["step_id"] == step_id]


def test_each_body_instance_carries_its_item(sf_with_workspace):
    sf = sf_with_workspace
    run_id = _prepare(sf, [["alpha", "beta", "gamma"]])
    _drive(sf, run_id)
    assert sf.get_run(run_id)["status"] == "completed"
    body = _rows(sf, run_id, "process_task")
    assert len(body) == 3
    # get_steps sorts by graph position then id, so instance order is claim order
    assert [s["loop_item"] for s in body] == ["alpha", "beta", "gamma"]


def test_a_step_outside_the_loop_has_no_item(sf_with_workspace):
    """NULL means "not in a loop", and must not be confused with "the loop's
    current item" — a drained loop keeps current_item set, and stamping it on
    an aggregator would attribute the aggregate to the last item."""
    sf = sf_with_workspace
    run_id = _prepare(sf, [["alpha", "beta"]])
    _drive(sf, run_id)
    assert _rows(sf, run_id, "prepare")[0]["loop_item"] is None
    after = _rows(sf, run_id, "all_done")
    assert after and after[0]["loop_item"] is None


def test_a_retried_body_step_keeps_its_item(sf_with_workspace):
    """A retry RE-claims the same row, so the stamp is written again. It must
    land on the item being retried, not on whatever the loop moved on to."""
    sf = sf_with_workspace
    run_id = _prepare(sf, [["alpha", "beta"]])
    sf.advance_run(run_id)
    first = sf.claim_next_step(run_id)
    assert first.step_id == "process_task"
    sf.fail_step(first.token, "boom")
    _drive(sf, run_id)
    body = _rows(sf, run_id, "process_task")
    retried = [s for s in body if s["retry_count"]]
    assert retried, "expected the failure to be retried"
    assert retried[0]["loop_item"] == "alpha"
    # every instance is attributed; none is left guessing
    assert {s["loop_item"] for s in body} == {"alpha", "beta"}


def test_the_column_is_exposed_without_asking_for_payloads(sf_with_workspace):
    """Hosts read the summary projection on every dashboard poll; an attribute
    only visible under include_payloads would cost them the whole context blob."""
    sf = sf_with_workspace
    run_id = _prepare(sf, [["alpha"]])
    _drive(sf, run_id)
    assert "loop_item" in sf.get_steps(run_id)[0]


def test_migration_adds_the_column_to_an_existing_db(tmp_path, mock_tools):
    """Boot replays the migration list every time; a DB written before this
    column existed must GAIN it rather than erroring on the first claim.

    The "old" table is the real DDL with the one line removed, so this stays a
    test of the migration and not of a hand-written toy schema.
    """
    import re
    import sqlite3
    from skillflow import schema

    previous_ddl = re.sub(r"^\s*loop_item\s+TEXT,\n", "", schema.SKILLFLOW_STEPS,
                          flags=re.MULTILINE)
    assert "loop_item" not in previous_ddl

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    for stmt in schema.ALL_DDL:
        conn.execute(previous_ddl if "skillflow_steps (" in stmt else stmt)
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(skillflow_steps)")}
    assert "loop_item" not in cols, "the pre-migration table already had it"
    conn.close()

    SkillFlow(str(db), tool_loader=mock_tools,
              workspace_base=str(tmp_path / "ws"),
              projects_base=str(tmp_path / "proj"))

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(skillflow_steps)")}
    conn.close()
    assert "loop_item" in cols
