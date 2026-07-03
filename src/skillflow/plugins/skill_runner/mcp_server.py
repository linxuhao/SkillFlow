"""skillflow-mcp — serve the runner protocol as MCP tools (stdio).

Any MCP-speaking agent (Claude Code, opencode, ...) can drive skillflow
pipelines with zero code changes on the agent side:

    { "mcpServers": { "skillflow": {
        "command": "skillflow-mcp",
        "args": ["--db", "~/.skillflow/skillflow.db",
                 "--workspace", "~/.skillflow/workspace",
                 "--graphs", "./graphs",
                 "--agent-configs", "./graphs/agents.yaml",
                 "--tools-dir", "./tools"] } } }

State lives in SQLite + the workspace (same model as the ``skillflow-run``
CLI): every tool call reconnects from ``run_id``, so a crashed client, a
second client, or an interleaved CLI call on the same run behave identically.
Stdio transport means there is no standing server to operate — the agent
spawns this process per session.

Security note: ``skillflow_tool`` proxies into ``sf.execute_tool``, which can
run registered native tools (pytest, lint, ...) in THIS process's environment.
Only wire this server to trusted clients over stdio, or behind auth.

Requires the optional dependency: ``pip install skillflow-py[mcp]``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_skillflow(db: str, workspace: str | None, projects: str | None,
                    graphs: list[str], agent_configs: list[str],
                    tools_dirs: list[str]):
    """Construct and register a SkillFlow instance for standalone serving."""
    import yaml

    from skillflow.core import SkillFlow
    from skillflow.graph import PipelineGraph
    from skillflow.tool_loader import ToolLoader

    loader = ToolLoader()
    for td in tools_dirs:
        loader.add_tools_dir(Path(td).expanduser())

    kwargs: dict = {"tool_loader": loader}
    if workspace:
        kwargs["workspace_base"] = str(Path(workspace).expanduser())
    if projects:
        kwargs["projects_base"] = str(Path(projects).expanduser())
    sf = SkillFlow(str(Path(db).expanduser()), **kwargs)

    # Agent configs first — graph registration validates the references.
    for path in agent_configs:
        data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for name, cfg in data.items():
                if isinstance(cfg, dict):
                    sf.register_agent_config_from_dict(name, cfg)

    for spec in graphs:
        p = Path(spec).expanduser()
        files = sorted(p.glob("*.yaml")) if p.is_dir() else [p]
        for f in files:
            sf.register_graph(PipelineGraph.from_yaml(f))

    return sf


def create_server(sf):
    """Build the FastMCP server over a configured SkillFlow instance."""
    from mcp.server.fastmcp import FastMCP

    from .service import RunnerService

    service = RunnerService(sf)
    mcp = FastMCP(
        "skillflow",
        instructions=(
            "Drive skillflow pipelines in runner mode: runner_start → do the "
            "work each instruction describes → runner_submit → repeat. A "
            "'paused' status is a checkpoint FOR YOUR HUMAN USER — present it "
            "and wait for their decision; never call runner_approve on your "
            "own. If a response carries validation_error, fix and re-submit. "
            "Use skillflow_tool only for tools named in a step instruction."
        ),
    )

    @mcp.tool()
    def runner_start(graph_name: str, project_id: str = "",
                     seeds: dict | None = None) -> dict:
        """Start a pipeline run. `seeds` = {filename: content} written to the
        graph's seed dir (e.g. {"task.md": "<the task>"}). Returns the first
        step's instruction; save run_id for every later call."""
        return service.start(graph_name, project_id=project_id or None,
                             seeds=seeds)

    @mcp.tool()
    def runner_next(run_id: str) -> dict:
        """Reconnect to a run and get the current instruction (use after a
        restart or if you lost track of state)."""
        return service.next(run_id)

    @mcp.tool()
    def runner_status(run_id: str) -> dict:
        """Read-only run status: state, current node, completed steps."""
        return service.status(run_id)

    @mcp.tool()
    def runner_submit(run_id: str, step_id: str,
                      result: dict | None = None) -> dict:
        """Submit the current step's outputs and advance. `result` carries one
        key per output slot (e.g. {"plan": "..."}); pass {} if you already
        wrote the outputs via skillflow_tool write_* calls."""
        return service.submit(run_id, step_id, result)

    @mcp.tool()
    def runner_approve(run_id: str) -> dict:
        """Approve a paused checkpoint. ONLY after the human user explicitly
        approved — the checkpoint is theirs, not yours."""
        return service.approve(run_id)

    @mcp.tool()
    def runner_reject(run_id: str, feedback: str, redirect_to: str = "") -> dict:
        """Reject a paused checkpoint with the user's feedback; the step
        re-runs with that feedback (redirect_to loops back to an earlier
        step instead)."""
        return service.reject(run_id, feedback, redirect_to)

    @mcp.tool()
    def skillflow_tool(run_id: str, step_id: str, name: str,
                       params: dict | None = None) -> dict:
        """Execute one of the CURRENT step's skillflow tools (write_<slot>,
        read_*, or a native tool named in the instruction). Not for your own
        host tools — call those directly."""
        return service.execute_step_tool(run_id, step_id, name, params)

    return mcp


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="skillflow-mcp",
        description="Serve skillflow's runner protocol as MCP tools over stdio.")
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--workspace", help="Workspace base directory")
    parser.add_argument("--projects", help="Projects base directory")
    parser.add_argument("--graphs", action="append", default=[],
                        help="Graph YAML file or directory (repeatable)")
    parser.add_argument("--agent-configs", action="append", default=[],
                        help="Agent-config YAML file (repeatable)")
    parser.add_argument("--tools-dir", action="append", default=[],
                        help="Native tools directory (repeatable)")
    args = parser.parse_args()

    try:
        import mcp  # noqa: F401
    except ImportError:
        print("skillflow-mcp requires the optional 'mcp' dependency: "
              "pip install skillflow-py[mcp]", file=sys.stderr)
        return 1

    sf = build_skillflow(args.db, args.workspace, args.projects,
                         args.graphs, args.agent_configs, args.tools_dir)
    server = create_server(sf)
    server.run()  # stdio transport
    return 0


if __name__ == "__main__":
    sys.exit(main())
