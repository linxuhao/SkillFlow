"""RunnerService — the transport-neutral core behind in-process, MCP and CLI."""

import pytest

from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph
from skillflow.plugins.skill_runner import RunnerService


PLAN_GATED = """
name: gated_task
begin: plan
end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: implement
      result: "completed"
      require_completed: true
steps:
  - id: plan
    step_type: agent
    agent_config: worker
    checkpoint: true
    checkpoint_label: "Plan Review"
    context:
      - source: { config: "gated_task", output: "task.md" }
    output:
      mode: content
      fixed:
        plan: "plan.md"
    transitions:
      - to: implement
        match: { from: checkpoint, value: approved }
  - id: implement
    step_type: agent
    agent_config: worker
    context:
      - source: { step: plan }
    output:
      mode: content
      fixed:
        summary: "summary.md"
    transitions:
      - to: null
"""


@pytest.fixture
def service(tmp_path):
    sf = SkillFlow(str(tmp_path / "sf.db"),
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "proj"))
    sf.register_agent_config_from_dict("worker", {"system_prompt": "do the work"})
    graph_file = tmp_path / "gated_task.yaml"
    graph_file.write_text(PLAN_GATED, encoding="utf-8")
    sf.register_graph(PipelineGraph.from_yaml(graph_file))
    return RunnerService(sf)


class TestStart:
    def test_unknown_graph(self, service):
        out = service.start("nope")
        assert "Unknown graph" in out["error"]

    def test_start_with_seed_reaches_context(self, service):
        out = service.start("gated_task", project_id="p1",
                            seeds={"task.md": "add sqrt function"})
        assert out["status"] == "in_progress"
        assert out["step"] == "plan"
        assert "add sqrt function" in out["instruction"]

    def test_concurrent_run_guard(self, service):
        first = service.start("gated_task", project_id="p1",
                              seeds={"task.md": "t"})
        assert first["status"] == "in_progress"
        second = service.start("gated_task", project_id="p1")
        assert "already live" in second["error"]
        assert first["run_id"] in second["error"]

    def test_seeds_require_project(self, service):
        out = service.start("gated_task", seeds={"task.md": "t"})
        assert "project_id" in out["error"]


class TestProtocolFlow:
    def test_full_gated_flow(self, service):
        run_id = service.start("gated_task", project_id="p1",
                               seeds={"task.md": "t"})["run_id"]

        # wrong step refused
        wrong = service.submit(run_id, "implement", {"summary": "x"})
        assert "Current step is 'plan'" in wrong["error"]

        paused = service.submit(run_id, "plan", {"plan": "## the plan"})
        assert paused["status"] == "paused"
        assert paused["checkpoint_label"] == "Plan Review"
        # engine holds the gate
        assert service.status(run_id)["status"] == "paused"

        # reject → feedback reaches the re-claimed plan step
        again = service.reject(run_id, "add tests")
        assert again["status"] == "in_progress"
        assert again["step"] == "plan"
        assert "add tests" in again["instruction"]

        service.submit(run_id, "plan", {"plan": "## plan v2"})
        released = service.approve(run_id)
        assert released["status"] == "in_progress"
        assert released["step"] == "implement"
        assert "## plan v2" in released["instruction"]  # prior step context

        done = service.submit(run_id, "implement", {"summary": "done"})
        assert done["status"] == "completed"
        assert done["outputs"]["implement"]["summary"] == "done"

    def test_reject_requires_feedback(self, service):
        run_id = service.start("gated_task", project_id="p1",
                               seeds={"task.md": "t"})["run_id"]
        assert "feedback is required" in service.reject(run_id, "  ")["error"]

    def test_unknown_run(self, service):
        assert "Run not found" in service.next("ghost")["error"]
        assert "Run not found" in service.status("ghost")["error"]

    def test_instruction_has_no_phantom_tool_ads(self, service):
        out = service.start("gated_task", project_id="p1",
                            seeds={"task.md": "t"})
        instr = out["instruction"]
        # slots described, not tool functions advertised
        assert "- `plan`" in instr
        assert "submit" in instr
        assert "### write_plan" not in instr
        assert "### create_plan" not in instr
        assert "### finish_step" not in instr
        assert "staging directory (`/" not in instr


class TestExecuteStepTool:
    def test_write_tool_stages_file(self, service):
        run_id = service.start("gated_task", project_id="p1",
                               seeds={"task.md": "t"})["run_id"]
        out = service.execute_step_tool(run_id, "plan", "write_plan",
                                        {"content": "## staged plan"})
        assert out.get("written")
        # submit with empty result — file already staged
        paused = service.submit(run_id, "plan", {})
        assert paused["status"] == "paused"
        plan_file = (service.sf._workspace.get_config_path("p1", "gated_task")
                     / "plan" / "plan.md")
        assert plan_file.read_text() == "## staged plan"

    def test_host_tool_is_redirected(self, service):
        run_id = service.start("gated_task", project_id="p1",
                               seeds={"task.md": "t"})["run_id"]
        out = service.execute_step_tool(run_id, "plan", "edit_file",
                                        {"path": "x.py"})
        assert "not a skillflow tool" in out["error"]
        assert "call it directly" in out["error"]

    def test_disallowed_skillflow_tool_names_allowed_set(self, service):
        run_id = service.start("gated_task", project_id="p1",
                               seeds={"task.md": "t"})["run_id"]
        out = service.execute_step_tool(run_id, "plan", "write_summary",
                                        {"content": "x"})
        assert "not allowed" in out["error"]

    def test_unknown_run_rejected(self, service):
        out = service.execute_step_tool("ghost", "plan", "write_plan", {})
        assert "Run not found" in out["error"]

    def test_finish_step_maps_to_submit(self, service):
        # finish_step through the proxy must CONFIRM the step — routed to
        # sf.execute_tool it returned a success-looking echo while leaving the
        # run stuck on a claimed step (found live).
        run_id = service.start("gated_task", project_id="p1",
                               seeds={"task.md": "t"})["run_id"]
        service.execute_step_tool(run_id, "plan", "write_plan",
                                  {"content": "## plan"})
        out = service.execute_step_tool(run_id, "plan", "finish_step",
                                        {"summary": "done"})
        assert out["status"] == "paused"  # step confirmed → checkpoint reached
        assert service.status(run_id)["status"] == "paused"
