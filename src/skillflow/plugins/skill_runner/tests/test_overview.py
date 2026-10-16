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
