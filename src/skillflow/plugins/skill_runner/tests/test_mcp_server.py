"""skillflow-mcp smoke test — drive a plan-gated run over real MCP stdio.

Spawns the server as a subprocess (exactly how an MCP client launches it) and
walks the protocol: list tools → start → submit → paused → approve → submit →
completed. Skipped when the optional 'mcp' dependency is absent.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

# Reuse the gated graph from the service tests (tests dir is not a package).
_spec = importlib.util.spec_from_file_location(
    "_service_tests", Path(__file__).parent / "test_service.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
PLAN_GATED = _mod.PLAN_GATED


AGENTS_YAML = "worker:\n  system_prompt: do the work\n"


def _result(res) -> dict:
    """Unwrap an MCP CallToolResult into the tool's dict payload."""
    if res.structuredContent is not None:
        # FastMCP wraps plain-dict returns as {"result": ...} only for
        # non-object returns; dict returns come through as-is.
        sc = res.structuredContent
        return sc.get("result", sc) if set(sc.keys()) == {"result"} else sc
    for block in res.content or []:
        if getattr(block, "type", "") == "text":
            return json.loads(block.text)
    raise AssertionError(f"no payload in tool result: {res}")


@pytest.fixture
def server_params(tmp_path):
    (tmp_path / "gated_task.yaml").write_text(PLAN_GATED, encoding="utf-8")
    (tmp_path / "agents.yaml").write_text(AGENTS_YAML, encoding="utf-8")
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "skillflow.plugins.skill_runner.mcp_server",
              "--db", str(tmp_path / "sf.db"),
              "--workspace", str(tmp_path / "ws"),
              "--projects", str(tmp_path / "proj"),
              "--graphs", str(tmp_path / "gated_task.yaml"),
              "--agent-configs", str(tmp_path / "agents.yaml")],
    )


async def test_full_gated_flow_over_mcp(server_params):
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = {t.name for t in (await session.list_tools()).tools}
            assert {"runner_start", "runner_next", "runner_status",
                    "runner_submit", "runner_approve", "runner_reject",
                    "skillflow_tool"} <= tools

            out = _result(await session.call_tool("runner_start", {
                "graph_name": "gated_task", "project_id": "p1",
                "seeds": {"task.md": "add sqrt"}}))
            assert out["status"] == "in_progress" and out["step"] == "plan"
            assert "add sqrt" in out["instruction"]
            run_id = out["run_id"]

            # write via the proxy, then submit with empty result
            staged = _result(await session.call_tool("skillflow_tool", {
                "run_id": run_id, "step_id": "plan", "name": "write_plan",
                "params": {"content": "## the plan"}}))
            assert staged.get("written")

            paused = _result(await session.call_tool("runner_submit", {
                "run_id": run_id, "step_id": "plan", "result": {}}))
            assert paused["status"] == "paused"
            assert paused["checkpoint_label"] == "Plan Review"

            # host-tool confusion gets redirected, not "not allowed"
            bounced = _result(await session.call_tool("skillflow_tool", {
                "run_id": run_id, "step_id": "plan", "name": "edit_file",
                "params": {}}))
            assert "not a skillflow tool" in bounced["error"]

            released = _result(await session.call_tool("runner_approve",
                                                       {"run_id": run_id}))
            assert released["status"] == "in_progress"
            assert released["step"] == "implement"
            assert "## the plan" in released["instruction"]

            done = _result(await session.call_tool("runner_submit", {
                "run_id": run_id, "step_id": "implement",
                "result": {"summary": "done"}}))
            assert done["status"] == "completed"
            assert done["outputs"]["implement"]["summary"] == "done"
