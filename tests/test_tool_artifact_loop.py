"""Tool artifact delivery and native loop progression share the agent path guarantees."""

from pathlib import Path

import pytest

from skillflow import SkillFlow, StepResult
from skillflow.exceptions import StaleClaimFenced
from skillflow.graph import PipelineGraph
from skillflow.tool_loader import ToolLoader


def _engine(tmp_path: Path) -> tuple[SkillFlow, ToolLoader]:
    loader = ToolLoader(Path(__file__).parents[1] / "src" / "skillflow" / "tools")
    engine = SkillFlow(
        str(tmp_path / "engine.db"),
        tool_loader=loader,
        workspace_base=str(tmp_path / "files"),
        artifact_history=False,
    )
    return engine, loader


def _advance_to_terminal(engine: SkillFlow, run_id: str) -> dict:
    for _ in range(15):
        run = engine.get_run(run_id)
        if run["status"] != "running":
            return run
        engine.advance_run(run_id)
    return engine.get_run(run_id)


@pytest.mark.parametrize("write,status", [(True, "completed"), (False, "failed")])
def test_declared_tool_artifact_is_published_before_downstream_reads(
    tmp_path: Path, write: bool, status: str
) -> None:
    engine, loader = _engine(tmp_path)

    def write_report(*, run_id: str, step_id: str, **_kwargs: object) -> dict:
        candidate = engine.output_directory(run_id, step_id)
        if write:
            (candidate / "report.md").write_text("native report")
        return {"written": True}

    loader.register_dynamic_tool("write_report", {}, write_report)
    engine.register_graph(
        PipelineGraph._from_dict(
            {
                "name": "tool_artifact",
                "begin": "write",
                "steps": [
                    {
                        "id": "write",
                        "step_type": "tool",
                        "tool_name": "write_report",
                        "output": {"mode": "write", "fixed": {"report": "report.md"}},
                        "transitions": [{"to": "done"}],
                    },
                    {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
                ],
            }
        )
    )
    run_id = engine.create_run("tool_artifact", context={}, project_id="execution")
    engine.start_run(run_id)
    run = _advance_to_terminal(engine, run_id)
    assert run["status"] == status
    published = engine._workspace.get_step_dir("execution", "tool_artifact", "write")
    if write:
        assert (published / "report.md").read_text() == "native report"
    else:
        assert "Declared tool output 'report' is missing" in run["error_reason"]
        assert not published.exists()
    assert not (engine.output_directory(run_id, "write") / "report.md").exists()


@pytest.mark.parametrize(
    "manifest,max_iterations,status,expected_items,dynamic,legacy",
    [
        ('{"items":["alpha","beta"]}', 3, "completed", ["alpha", "beta"], False, False),
        ('{"items":["alpha","beta"]}', 2, "completed", ["alpha", "beta"], False, False),
        ('{"items":["alpha","beta"]}', 1, "failed", [], False, False),
        ('{"items":[]}', 1, "completed", [], False, False),
        ('{"items":["alpha"]}', 1, "failed", ["alpha"], True, False),
        ('{"items":["beta"]}', 1, "failed", [], False, True),
        ('{"items":', 3, "failed", [], False, False),
        ('{"items":"alpha"}', 3, "failed", [], False, False),
    ],
)
def test_tool_loop_body_credits_each_item_once(
    tmp_path: Path,
    manifest: str,
    max_iterations: int,
    status: str,
    expected_items: list[str],
    dynamic: bool,
    legacy: bool,
) -> None:
    engine, loader = _engine(tmp_path)
    seen: list[str | None] = []

    def body(*, task_name: str | None = None, **_kwargs: object) -> dict:
        seen.append(task_name)
        if dynamic:
            source = engine._workspace.get_step_dir("execution", "tool_loop", "plan") / "items.json"
            source.write_text('{"items":["beta"]}')
        return {}

    loader.register_dynamic_tool("body", {}, body)
    engine.register_agent_config("planner", tools=[])
    engine.register_graph(
        PipelineGraph._from_dict(
            {
                "name": "tool_loop",
                "begin": "plan",
                "steps": [
                    {
                        "id": "plan",
                        "step_type": "agent",
                        "agent_config": "planner",
                        "output": {"mode": "write"},
                        "transitions": [{"to": "each"}],
                    },
                    {
                        "id": "each",
                        "step_type": "loop",
                        "loop": {
                            "source": {"step": "plan", "file": "items.json", "field": "items"},
                            "item_as": "item",
                            "max_iterations": max_iterations,
                        },
                        "transitions": [{"to": "body", "max_loop": 3}, {"to": "done"}],
                    },
                    {
                        "id": "body",
                        "step_type": "tool",
                        "tool_name": "body",
                        "transitions": [{"to": "each", "max_loop": 3}],
                    },
                    {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
                ],
            }
        )
    )
    run_id = engine.create_run("tool_loop", context={}, project_id="execution")
    engine.start_run(run_id)
    engine.advance_run(run_id)
    claim = engine.claim_next_step(run_id)
    assert claim and claim.step_id == "plan"
    (engine.output_directory(run_id, "plan") / "items.json").write_text(manifest)
    engine.confirm_step(claim.token, StepResult(outputs={}))
    if legacy:
        with engine._tx() as conn:
            conn.execute(
                "INSERT INTO skillflow_loop_state "
                "(run_id, loop_step_id, items_json, completed_items, current_item, item_context_key) "
                "VALUES (?, 'each', '[\"alpha\"]', '[\"alpha\"]', NULL, 'item')",
                (run_id,),
            )
    run = _advance_to_terminal(engine, run_id)
    assert run["status"] == status
    assert seen == expected_items
    if status == "failed":
        assert "Loop 'each'" in run["error_reason"]


@pytest.mark.parametrize("limit", [0, -1, True, "2"])
def test_loop_rejects_nonpositive_or_noninteger_limit(limit: object) -> None:
    with pytest.raises(ValueError, match="loop.max_iterations must be a positive integer"):
        PipelineGraph._from_dict(
            {
                "name": "invalid_loop_limit",
                "begin": "each",
                "steps": [
                    {
                        "id": "each",
                        "step_type": "loop",
                        "loop": {"source": {"step": "plan", "file": "items.json", "field": "items"},
                                 "max_iterations": limit},
                        "transitions": [{"to": None}],
                    }
                ],
            }
        )


def test_tool_artifact_validation_failure_does_not_publish(tmp_path: Path) -> None:
    engine, loader = _engine(tmp_path)

    def write_invalid(*, run_id: str, step_id: str, **_kwargs: object) -> dict:
        (engine.output_directory(run_id, step_id) / "report.json").write_text('{"wrong":true}')
        return {}

    loader.register_dynamic_tool("write_invalid", {}, write_invalid)
    engine.register_graph(PipelineGraph._from_dict({
        "name": "validate_tool", "begin": "write",
        "steps": [
            {"id": "write", "step_type": "tool", "tool_name": "write_invalid",
             "output": {"mode": "write", "fixed": {"report": "report.json"}},
             "validation": [{"files": ["report.json"], "tool": "json_schema",
                             "inline_schema": {"required": ["name"]}}],
             "transitions": [{"to": None}]},
        ],
    }))
    run_id = engine.create_run("validate_tool", context={}, project_id="execution")
    engine.start_run(run_id)
    run = _advance_to_terminal(engine, run_id)
    assert run["status"] == "failed"
    assert "Validation failed" in run["error_reason"]
    assert not (engine._workspace.get_step_dir("execution", "validate_tool", "write")
                / "report.json").exists()


def test_reclaimed_tool_cannot_publish_previous_claim(tmp_path: Path) -> None:
    engine, loader = _engine(tmp_path)

    def stale_writer(*, run_id: str, step_id: str, **_kwargs: object) -> dict:
        (engine.output_directory(run_id, step_id) / "report.md").write_text("stale")
        with engine._tx() as conn:
            conn.execute(
                "UPDATE skillflow_steps SET claim_epoch=claim_epoch+1, version=version+1 "
                "WHERE run_id=? AND step_id=? AND status='claimed'", (run_id, step_id))
        return {}

    loader.register_dynamic_tool("stale_writer", {}, stale_writer)
    engine.register_graph(PipelineGraph._from_dict({
        "name": "fenced_tool", "begin": "write",
        "steps": [
            {"id": "write", "step_type": "tool", "tool_name": "stale_writer",
             "output": {"mode": "write", "fixed": {"report": "report.md"}},
             "transitions": [{"to": None}]},
        ],
    }))
    run_id = engine.create_run("fenced_tool", context={}, project_id="execution")
    engine.start_run(run_id)
    with pytest.raises(StaleClaimFenced):
        engine.advance_run(run_id)
    assert engine.get_run(run_id)["status"] == "running"
    assert not (engine._workspace.get_step_dir("execution", "fenced_tool", "write")
                / "report.md").exists()


def test_recovery_during_validation_cannot_publish_pending_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, loader = _engine(tmp_path)

    def write_report(*, run_id: str, step_id: str, **_kwargs: object) -> dict:
        (engine.output_directory(run_id, step_id) / "report.md").write_text("candidate")
        return {}

    def recover(files: list[str], *, run_id: str, **_kwargs: object) -> dict:
        assert files == ["report.md"]
        monkeypatch.setattr("skillflow.core.owner_is_dead", lambda _owner: True)
        assert engine.recover_stale_claims(0, run_id=run_id) == [run_id]
        return {"passed": True}

    loader.register_dynamic_tool("write_report", {}, write_report)
    loader.register_dynamic_tool("recover", {}, recover)
    engine.register_graph(PipelineGraph._from_dict({
        "name": "validation_recovery", "begin": "write",
        "steps": [
            {"id": "write", "step_type": "tool", "tool_name": "write_report",
             "output": {"mode": "write", "fixed": {"report": "report.md"}},
             "validation": [{"files": ["report.md"], "tool": "recover"}],
             "transitions": [{"to": None}]},
        ],
    }))
    run_id = engine.create_run("validation_recovery", context={}, project_id="execution")
    engine.start_run(run_id)
    with pytest.raises(StaleClaimFenced):
        engine.advance_run(run_id)
    assert engine.get_steps(run_id)[0]["status"] == "pending"
    assert engine.get_run(run_id)["status"] == "running"
    assert (engine.output_directory(run_id, "write") / "report.md").read_text() == "candidate"
    assert not (engine._workspace.get_step_dir("execution", "validation_recovery", "write")
                / "report.md").exists()


def test_admitted_tool_delivery_blocks_recovery_until_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, loader = _engine(tmp_path)

    def write_report(*, run_id: str, step_id: str, **_kwargs: object) -> dict:
        (engine.output_directory(run_id, step_id) / "report.md").write_text("delivered")
        return {}

    loader.register_dynamic_tool("write_report", {}, write_report)
    engine.register_graph(PipelineGraph._from_dict({
        "name": "admitted_delivery", "begin": "write",
        "steps": [
            {"id": "write", "step_type": "tool", "tool_name": "write_report",
             "output": {"mode": "write", "fixed": {"report": "report.md"}},
             "transitions": [{"to": "done"}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }))
    run_id = engine.create_run("admitted_delivery", context={}, project_id="execution")
    engine.start_run(run_id)
    original_commit = engine._step_commit

    def during_commit(token):
        monkeypatch.setattr("skillflow.core.owner_is_dead", lambda _owner: True)
        assert engine.recover_stale_claims(0, run_id=run_id) == []
        return original_commit(token)

    monkeypatch.setattr(engine, "_step_commit", during_commit)
    assert _advance_to_terminal(engine, run_id)["status"] == "completed"
    assert (engine._workspace.get_step_dir("execution", "admitted_delivery", "write")
            / "report.md").read_text() == "delivered"


def test_promoted_tool_keeps_delivery_intent_when_completion_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, loader = _engine(tmp_path)

    def write_report(*, run_id: str, step_id: str, **_kwargs: object) -> dict:
        (engine.output_directory(run_id, step_id) / "report.md").write_text("published")
        return {}

    loader.register_dynamic_tool("write_report", {}, write_report)
    engine.register_graph(PipelineGraph._from_dict({
        "name": "unknown_delivery", "begin": "write",
        "steps": [
            {"id": "write", "step_type": "tool", "tool_name": "write_report",
             "output": {"mode": "write", "fixed": {"report": "report.md"}},
             "transitions": [{"to": "done"}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }))
    run_id = engine.create_run("unknown_delivery", context={}, project_id="execution")
    engine.start_run(run_id)

    def fail_completion(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("completion write interrupted")

    monkeypatch.setattr(engine, "_confirm_tool_in_tx", fail_completion)
    with pytest.raises(RuntimeError, match="completion write interrupted"):
        engine.advance_run(run_id)
    published = engine._workspace.get_step_dir("execution", "unknown_delivery", "write")
    assert (published / "report.md").read_text() == "published"
    assert engine.get_steps(run_id)[0]["status"] == "claimed"
    with engine._ro() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM skillflow_active_ops WHERE run_id=? AND kind='delivery'",
            (run_id,),
        ).fetchone()[0] == 1
    monkeypatch.setattr("skillflow.core.owner_is_dead", lambda _owner: True)
    assert engine.recover_stale_claims(0, run_id=run_id) == []
    assert engine.get_steps(run_id)[0]["status"] == "claimed"
    assert (published / "report.md").read_text() == "published"


def test_declared_tool_artifact_rejects_ignored_lifecycle_only() -> None:
    base = {"id": "tool", "step_type": "tool", "tool_name": "noop",
            "lifecycle": {"on_deliver": {"tool": "noop"}}}
    PipelineGraph._from_dict({"name": "legacy_tool", "begin": "tool", "steps": [base]})
    with pytest.raises(ValueError, match="declared tool artifact output cannot use lifecycle"):
        PipelineGraph._from_dict({"name": "artifact_tool", "begin": "tool", "steps": [
            {**base, "output": {"mode": "write", "fixed": {"report": "report.md"}}}
        ]})
