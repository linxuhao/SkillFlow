"""Pipeline overview: every runner instruction maps the whole graph first."""

import tomllib
from pathlib import Path

from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph
from skillflow.plugins.skill_runner import RunnerService
from skillflow.plugins.skill_runner.runner import pipeline_overview

GRAPH = """
name: ov
description: "Write then check"
begin: write
end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: done
      result: completed
steps:
  - id: write
    step_type: agent
    agent_config: worker
    output: { mode: content, fixed: { text: "text.md" } }
    transitions:
      - to: check
  - id: check
    step_type: agent
    agent_config: worker
    checkpoint: true
    checkpoint_label: "Approve the text"
    output: { mode: content, fixed: { note: "note.md" } }
    transitions:
      - to: done
        match: { from: checkpoint, value: approved }
  - id: done
    step_type: agent
    agent_config: worker
"""


def _service(tmp_path, text=GRAPH):
    sf = SkillFlow(str(tmp_path / "sf.db"), workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "proj"))
    sf.register_agent_config_from_dict("worker", {"system_prompt": "work"})
    f = tmp_path / "g.yaml"
    f.write_text(text, encoding="utf-8")
    sf.register_graph(PipelineGraph.from_yaml(f))
    return RunnerService(sf)


def test_instruction_starts_with_whole_pipeline(tmp_path):
    svc = _service(tmp_path)
    out = svc.start("ov", project_id="p")
    ins = out["instruction"]
    assert ins.startswith("## Pipeline overview")
    assert ins.index("## Pipeline overview") < ins.index("## Task: Write")
    for sid in ("`write`", "`check`", "`done`"):
        assert sid in ins
    assert "1. ▶ `write`" in ins
    assert 'human checkpoint: "Approve the text"' in ins
    assert "You are on step 1 of 3" in ins


def test_position_and_done_counts_advance(tmp_path):
    svc = _service(tmp_path)
    rid = svc.start("ov", project_id="p")["run_id"]
    out = svc.submit(rid, "write", {"text": "t"})
    ins = out["instruction"]
    assert "2. ▶ `check`" in ins
    assert "`write` — agent → `check` [done ×1]" in ins
    assert "completed step executions so far: 1" in ins


def test_loop_bounds_and_conditions_are_shown():
    g = PipelineGraph._from_dict({
        "name": "loop", "begin": "a",
        "steps": [
            {"id": "a", "step_type": "agent", "agent_config": "w",
             "transitions": [{"to": "b"}]},
            {"id": "b", "step_type": "agent", "agent_config": "w",
             "transitions": [{"to": None, "match": {"ok": True}},
                             {"to": "a", "match": {"ok": False}, "max_loop": 3}]},
        ]})
    text = pipeline_overview(g, "b", {})
    assert "end if ok=true" in text
    assert "`a` if ok=false (at most 3 times)" in text


def test_large_graph_is_windowed_around_current_step():
    steps = [{"id": f"s{i}", "step_type": "agent", "agent_config": "w",
              "transitions": [{"to": f"s{i + 1}"}]} for i in range(79)]
    steps.append({"id": "s79", "step_type": "agent", "agent_config": "w"})
    g = PipelineGraph._from_dict({"name": "big", "begin": "s0", "steps": steps})
    text = pipeline_overview(g, "s40", {})
    numbered = [l for l in text.splitlines() if l[:1].isdigit()]
    assert len(numbered) <= 30
    assert "▶ `s40`" in text and "`s0`" in text and "`s79`" in text
    assert "more steps" in text
    assert "You are on step 41 of 80" in text


def test_mcp_extra_excludes_mcp_2():
    """mcp 2.x removed FastMCP, which skillflow-mcp imports."""
    root = Path(__file__).resolve().parents[5]
    extra = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "optional-dependencies"]["mcp"]
    assert any(spec.replace(" ", "").startswith("mcp") and "<2" in spec
               for spec in extra)


def test_graphs_dir_skips_its_agents_yaml(tmp_path):
    """README layout: --graphs ./graphs --agent-configs ./graphs/agents.yaml."""
    from skillflow.plugins.skill_runner.mcp_server import build_skillflow
    gdir = tmp_path / "graphs"
    gdir.mkdir()
    (gdir / "ov.yaml").write_text(GRAPH, encoding="utf-8")
    (gdir / "agents.yaml").write_text("worker:\n  system_prompt: work\n",
                                      encoding="utf-8")
    sf = build_skillflow(str(tmp_path / "sf.db"), str(tmp_path / "ws"), None,
                         [str(gdir)], [str(gdir / "agents.yaml")], [])
    assert set(sf._graphs) == {"ov"}


def _edges_graph(transitions, **node):
    return PipelineGraph._from_dict({
        "name": "e", "begin": "a",
        "steps": [{"id": "a", "step_type": "agent", "agent_config": "w",
                   "transitions": transitions, **node},
                  {"id": "b", "step_type": "agent", "agent_config": "w"}]})


def test_field_value_match_names_the_flag_not_the_keywords():
    g = _edges_graph([{"to": "b", "match": {"field": "approved", "value": True}}])
    text = pipeline_overview(g, "a", {})
    assert "`b` if approved=true" in text
    assert "field=" not in text


def test_from_file_match_points_at_the_output_file():
    g = _edges_graph([{"to": "b", "match": {"from_file": "verdict.json",
                                             "field": "passed", "value": True},
                       "label": "review passed"}])
    text = pipeline_overview(g, "a", {})
    assert "`b` if passed=true in your `verdict.json`" in text
    assert '"review passed"' in text
    assert "from_file=" not in text


def test_checkpoint_reject_target_is_shown():
    g = _edges_graph([{"to": "b", "match": {"from": "checkpoint", "value": "approved"}}],
                     checkpoint=True, checkpoint_label="OK?",
                     checkpoint_reject_to="a")
    assert 'human checkpoint: "OK?"; a rejection returns to `a`' in \
        pipeline_overview(g, "a", {})


def test_tool_steps_say_who_runs_them():
    g = PipelineGraph._from_dict({"name": "t", "begin": "lint", "steps": [
        {"id": "lint", "step_type": "tool", "tool_name": "lint",
         "transitions": [{"to": None}]}]})
    assert "tool `lint` (you run it)" in pipeline_overview(g, "lint", {})
    assert "tool `lint` (automatic)" in pipeline_overview(
        g, "lint", {}, tools_delegated=False)


def test_every_fixture_renders():
    fixtures = Path(__file__).resolve().parents[5] / "tests" / "fixtures"
    rendered = 0
    for f in sorted(fixtures.glob("*.yaml")):
        g = PipelineGraph.from_yaml(f)
        text = pipeline_overview(g, g.begin, {})
        assert text.startswith("## Pipeline overview")
        rendered += 1
    assert rendered > 10


def test_custom_assembler_can_opt_out(tmp_path):
    from skillflow.plugins.skill_runner.runner import PromptAssembler, SkillTool
    svc = _service(tmp_path)

    class Plain(PromptAssembler):
        include_overview = False

    rid = svc.start("ov", project_id="p")["run_id"]
    tool = SkillTool(svc.sf, "ov", prompt_assembler=Plain())
    resp = tool(action="next", run_id=rid)
    assert resp.step == "write"
    assert "Pipeline overview" not in resp.instruction


def test_paused_instruction_uses_pinned_overview_and_position(tmp_path):
    svc = _service(tmp_path)
    rid = svc.start("ov", project_id="p")["run_id"]
    svc.submit(rid, "write", {"text": "actual draft"})
    paused = svc.submit(rid, "check", {"note": "actual retained review"})
    assert paused["status"] == "paused" and paused["step"] == "check"
    assert paused["instruction"].startswith("## Pipeline overview")
    assert "2. ▶ `check`" in paused["instruction"]
    assert "You are on step 2 of 3" in paused["instruction"]
    assert "completed step executions so far: 2" in paused["instruction"]
    assert "Pipeline paused at checkpoint: Approve the text" in paused["instruction"]
    assert paused["outputs"] == {"note": "actual retained review"}
    original = svc.sf.get_run(rid)["graph_version"]
    changed = PipelineGraph._from_dict({"name": "ov", "begin": "replacement",
        "steps": [{"id": "replacement", "step_type": "agent"}]})
    svc.sf.register_graph(changed)
    resumed = svc.next(rid)
    assert resumed["instruction"] == paused["instruction"]
    assert "replacement" not in resumed["instruction"]
    assert svc.sf.get_run(rid)["graph_version"] == original


def test_custom_assembler_overview_opt_out_applies_to_pause(tmp_path):
    from skillflow.plugins.skill_runner.runner import PromptAssembler, SkillTool
    class Plain(PromptAssembler):
        include_overview = False
    svc = _service(tmp_path)
    rid = svc.start("ov", project_id="p")["run_id"]
    svc.submit(rid, "write", {"text": "draft"})
    svc.submit(rid, "check", {"note": "review"})
    response = SkillTool(svc.sf, "ov", prompt_assembler=Plain())(run_id=rid)
    assert response.status == "paused"
    assert response.instruction.startswith("Pipeline paused at checkpoint:")
    assert "Pipeline overview" not in response.instruction
