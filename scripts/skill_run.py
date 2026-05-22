#!/usr/bin/env python3
"""Stateless CLI runner for skillflow pipelines.

An LLM agent calls this via shell to drive a pipeline interactively.
Each invocation creates fresh SkillFlow + SkillTool instances; state
persists in a file-based SQLite DB via run_id reconnection.

Usage:
    skillflow-run --graph pipeline.yaml --action next
    skillflow-run --action submit --run-id <id> --result '{"key": "val"}'
    skillflow-run --action approve --run-id <id>
    skillflow-run --action reject --run-id <id> --feedback "reason"
    skillflow-run --action abort --run-id <id>
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure package is importable when run from repo without pip install
_repo_root = Path(__file__).parent.parent
_src = _repo_root / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph
from skillflow.tool_loader import ToolLoader
from skillflow.plugins.skill_runner import SkillTool


def main():
    parser = argparse.ArgumentParser(
        description="Stateless skillflow pipeline runner — an LLM agent calls this "
                    "via shell to drive a pipeline step by step.",
        epilog=(
            "Examples:\n"
            "  skillflow-run --graph pipeline.yaml --action next\n"
            "  skillflow-run --action submit --run-id <id> --result '{\"key\":\"val\"}'\n"
            "  skillflow-run --action approve --run-id <id>\n"
            "  skillflow-run --action reject --run-id <id> --feedback \"reason\"\n"
            "  skillflow-run --action next --run-id <id>         # reconnect after restart\n"
            "\n"
            "Workflow:\n"
            "  1. Start:    --graph <file> --action next         → returns JSON with run_id + step\n"
            "  2. Submit:   --action submit --run-id <id> --result '{...}'\n"
            "  3. Approve:  --action approve --run-id <id>       (at checkpoints)\n"
            "  4. Reject:   --action reject --run-id <id> --feedback \"reason\"\n"
            "  5. Reconnect: --action next --run-id <id>          (after restart, no --graph needed)\n"
            "\n"
            'State is persisted in ~/.skillflow/runs.db (SQLite).'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--graph", help="Pipeline YAML file. Required on first call, omitted on resume.")
    parser.add_argument("--db", default=os.path.expanduser("~/.skillflow/runs.db"),
                        help="SQLite DB path (default: ~/.skillflow/runs.db)")
    parser.add_argument("--action", default="next",
                        choices=["next", "submit", "approve", "reject", "abort"],
                        help="Action: next (start/advance), submit (confirm step), "
                             "approve/reject (checkpoint), abort (cancel run)")
    parser.add_argument("--run-id", default="", help="Run ID from previous JSON response")
    parser.add_argument("--step-id", default="", help="Step ID from response (for approve/reject)")
    parser.add_argument("--result", default="{}", help="Result JSON for submit (e.g. '{\"issues\":[]}')")
    parser.add_argument("--feedback", default="", help="Feedback message for reject")
    parser.add_argument("--delegate-tools", action="store_true",
                        help="Delegate unknown tools to the agent instead of auto-executing")
    parser.add_argument("--workspace", default=os.path.expanduser("~/.skillflow/workspaces"),
                        help="Workspace base path (default: ~/.skillflow/workspaces)")
    parser.add_argument("--project-id", default="",
                        help="Project ID for workspace-scoped runs")
    args = parser.parse_args()

    # Validate required argument combinations
    if args.action in ("submit", "approve", "reject", "abort") and not args.run_id:
        parser.error(f"--action {args.action} requires --run-id")
    if args.action == "next" and not args.graph and not args.run_id:
        parser.error("--action next requires --graph (first call) or --run-id (resume)")

    # Ensure DB directory exists
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Bootstrap: graph is required on first call, optional on resume
    graph_name = ""
    project_id = args.project_id
    if args.graph:
        graph = PipelineGraph.from_yaml(args.graph)
        graph_name = graph.name

    # Init SkillFlow with file DB + tool loader + workspace
    loader = ToolLoader()
    sf = SkillFlow(
        str(db_path),
        tool_loader=loader,
        delegate_tools_to_agent=args.delegate_tools,
        workspace_base=args.workspace,
        projects_base=os.path.join(args.workspace, "projects"),
    )

    # Register graph if provided
    if args.graph:
        # Register agent configs referenced by the graph
        for step in graph.steps:
            if step.agent_config:
                try:
                    sf.register_agent_config(step.agent_config, model="host")
                except Exception:
                    pass
        try:
            sf.register_graph(graph)
        except Exception:
            pass  # Already registered

        # If this is the converter pipeline, register converter agents
        if "skill_converter" in str(args.graph):
            try:
                from skillflow.plugins.skill_converter.converter import _register_converter_agents
                _register_converter_agents(sf)
            except Exception:
                pass

    # If resuming, resolve graph_name from the run
    if not graph_name and args.run_id:
        run = sf.get_run(args.run_id)
        if run:
            graph_name = run.get("graph_name", "")

    # Parse result JSON
    result = {}
    try:
        result = json.loads(args.result)
    except json.JSONDecodeError:
        print(json.dumps({"status": "failed", "error": f"Invalid JSON: {args.result}"}))
        sys.exit(1)

    # Execute
    tool = SkillTool(sf, graph_name, project_id=project_id or None)
    resp = tool(
        action=args.action,
        run_id=args.run_id or "",
        step_id=args.step_id,
        result=result if result else None,
        feedback=args.feedback,
    )

    # Output response as JSON
    output = {
        "status": resp.status,
        "run_id": resp.run_id,
        "step": resp.step,
        "instruction": resp.instruction,
        "tools": resp.tools,
        "tool_name": resp.tool_name,
        "tool_params": resp.tool_params,
        "checkpoint_label": resp.checkpoint_label,
        "outputs": resp.outputs,
        "error": resp.error,
        "steps_completed": resp.steps_completed,
    }
    print(json.dumps(output))

    if resp.status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
