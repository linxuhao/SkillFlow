"""skillflow-run submit through the real CLI entry point.

The CLI used to confirm a step with the result dict as step outputs only, never
staging the slot files: a validated content step failed with "File not found"
and the next step's context did not contain the submitted text. Each call
here is a fresh process, exactly as an agent drives it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

GRAPH = """
name: cli_review
description: "Draft, review, finish"
begin: draft
end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: done
      result: completed
steps:
  - id: draft
    step_type: agent
    agent_config: writer
    context:
      - source: { from: "workspace", path: "brief.md" }
    output:
      mode: content
      fixed:
        draft: { file: "draft.md" }
    transitions:
      - to: review
  - id: review
    step_type: agent
    agent_config: reviewer
    context:
      - source: { step: draft }
    output:
      mode: content
      fixed:
        verdict: { file: "verdict.json", format: '{"approved": bool, "issues": [str, ...]}' }
    validation:
      - files: ["verdict.json"]
        tool: json_schema
        inline_schema:
          type: object
          required: [approved, issues]
          properties:
            approved: { type: boolean }
    transitions:
      - to: done
        match: { approved: true }
      - to: draft
        match: { approved: false }
        max_loop: 2
  - id: done
    step_type: agent
    agent_config: writer
"""


def _cli(tmp_path, *args):
    cmd = [sys.executable, "-m", "skillflow.run_cli",
           "--db", str(tmp_path / "runs.db"),
           "--workspace", str(tmp_path / "ws"),
           "--project-id", "p1", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _start(tmp_path):
    graph = tmp_path / "cli_review.yaml"
    graph.write_text(GRAPH, encoding="utf-8")
    brief = tmp_path / "ws" / "p1" / "brief.md"
    brief.parent.mkdir(parents=True)
    brief.write_text("BRIEF-TEXT", encoding="utf-8")
    out = _cli(tmp_path, "--action", "start", "--graph", str(graph))
    assert out["status"] == "in_progress" and out["step"] == "draft"
    return out["run_id"]


def _submit(tmp_path, run_id, result, *extra):
    return _cli(tmp_path, "--action", "submit", "--run-id", run_id,
                "--result", json.dumps(result), *extra)


def test_submitted_slot_reaches_next_step_context(tmp_path):
    rid = _start(tmp_path)
    out = _submit(tmp_path, rid, {"draft": "DRAFT-MARKER-1"})
    assert out["step"] == "review"
    assert "DRAFT-MARKER-1" in out["instruction"]


def test_validated_structured_slot_passes_first_submit(tmp_path):
    rid = _start(tmp_path)
    _submit(tmp_path, rid, {"draft": "d"})
    out = _submit(tmp_path, rid, {"verdict": {"approved": True, "issues": []},
                                  "approved": True})
    assert out["validation_error"] == ""
    assert out["status"] == "completed"  # reaching `done` ends the run
    verdict = next((tmp_path / "ws").rglob("verdict.json"))
    assert json.loads(verdict.read_text(encoding="utf-8"))["approved"] is True


def test_structured_slot_accepts_json_string(tmp_path):
    rid = _start(tmp_path)
    _submit(tmp_path, rid, {"draft": "d"})
    out = _submit(tmp_path, rid, {"verdict": '{"approved": false, "issues": ["x"]}',
                                  "approved": False})
    assert out["validation_error"] == ""
    assert out["step"] == "draft"


def test_invalid_slot_is_a_validation_error_naming_the_slot(tmp_path):
    rid = _start(tmp_path)
    _submit(tmp_path, rid, {"draft": "d"})
    out = _submit(tmp_path, rid, {"verdict": "not json", "approved": True})
    assert out["status"] == "in_progress"
    assert out["step"] == "review"
    assert "verdict" in out["validation_error"]
    assert "File not found" not in out["validation_error"]


def test_wrong_step_id_reports_the_waiting_step(tmp_path):
    rid = _start(tmp_path)
    out = _submit(tmp_path, rid, {"verdict": {"approved": True, "issues": []}},
                  "--step-id", "review")
    assert out["step"] == "draft"
    assert "not 'review'" in out["error"]


def test_convert_first_step_submits(tmp_path):
    """skillflow-convert wraps the same CLI; its first (structured) step must
    be deliverable too."""
    env = {**os.environ, "HOME": str(tmp_path)}
    def convert(*args):
        proc = subprocess.run([sys.executable, "-m", "skillflow.convert_cli", *args],
                              capture_output=True, text=True, env=env, timeout=120)
        return json.loads(proc.stdout.strip().splitlines()[-1])
    out = convert("--desc", "Review a pull request and summarise issues.",
                  "--action", "start")
    assert out["step"] == "analyze_skill"
    analysis = {"phases": ["read", "summarise"], "decisions": [],
                "terminal_condition": "summary written",
                "tools_per_phase": {}, "checkpoints": []}
    out = convert("--action", "submit", "--run-id", out["run_id"],
                  "--result", json.dumps({"analysis": analysis}))
    assert out["validation_error"] == ""
    assert out["step"] == "design_graph"
    assert "summary written" in out["instruction"]
    assert list(Path(tmp_path, ".skillflow").rglob("skill_analysis.json"))


def test_cli_checkpoint_overview_and_exact_approval_replay(tmp_path):
    import sqlite3
    graph = tmp_path / "checkpoint.yaml"
    graph.write_text(GRAPH.replace("    agent_config: writer", "    agent_config: writer\n    checkpoint: true", 1))
    started = _cli(tmp_path, "--action", "start", "--graph", str(graph))
    rid = started["run_id"]
    paused = _submit(tmp_path, rid, {"draft": "CLI RETAINED CHECKPOINT"})
    assert paused["status"] == "paused"
    assert paused["instruction"].startswith("## Pipeline overview")
    assert paused["outputs"] == {"draft": "CLI RETAINED CHECKPOINT"}
    token = paused["expected_checkpoint"]
    bad = dict(token, version=token["version"] + 1)
    refused = _cli(tmp_path, "--action", "approve", "--run-id", rid,
        "--expected-checkpoint", json.dumps(bad), "--decision-id", "cli-decision")
    assert refused["status"] == "failed" and "Stale" in refused["error"]
    approved = _cli(tmp_path, "--action", "approve", "--run-id", rid,
        "--expected-checkpoint", json.dumps(token), "--decision-id", "cli-decision")
    assert approved["status"] == "approved" and approved["step"] == "review"
    assert _cli(tmp_path, "--action", "next", "--run-id", rid)["step"] == "review"
    def rows():
        with sqlite3.connect(tmp_path / "runs.db") as db:
            return db.execute("SELECT * FROM skillflow_steps ORDER BY id").fetchall()
    before = rows()
    replay = _cli(tmp_path, "--action", "approve", "--run-id", rid,
        "--expected-checkpoint", json.dumps(token), "--decision-id", "cli-decision")
    assert replay == approved
    assert rows() == before
