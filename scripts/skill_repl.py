#!/usr/bin/env python3
"""Interactive runner for skillflow pipelines.

Drives a pipeline as a human agent with action="next"/"submit"/"approve"/"reject".
Native tools auto-execute; custom tools are delegated to the user.

Usage:
    skillflow-run <graph.yaml>
    python3 scripts/skill_repl.py <graph.yaml>
"""

import json
import os
import sys
import tempfile
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
    graph_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not graph_path:
        print("Usage: skillflow-run <graph.yaml>")
        sys.exit(1)

    tmp = tempfile.mkdtemp(prefix="skillflow_repl_")

    # Load native tools + plugin tools
    loader = ToolLoader()
    import skillflow.plugins
    loader.add_tools_dir(str(Path(skillflow.plugins.__path__[0]) / "linter" / "tools"))

    sf = SkillFlow(
        ":memory:",
        tool_loader=loader,
        delegate_tools_to_agent=True,
        workspace_base=os.path.join(tmp, "ws"),
        projects_base=os.path.join(tmp, "projects"),
    )

    graph = PipelineGraph.from_yaml(graph_path)

    # Register agent configs referenced by the graph
    for step in graph.steps:
        if step.agent_config:
            try:
                sf.register_agent_config(step.agent_config, model="host")
            except Exception:
                pass

    # Register converter agents if running the converter pipeline
    if "skill_converter" in graph_path:
        try:
            from skillflow.plugins.skill_converter.converter import _register_converter_agents
            _register_converter_agents(sf)
        except Exception:
            pass

    sf.register_graph(graph)

    print(f"Pipeline: {graph.name} ({len(graph.steps)} steps)")
    print(f"Begin: {graph.begin}")
    print()

    tool = SkillTool(sf, graph.name)

    # ── Interactive loop ──────────────────────────────────────────
    resp = tool(action="next")

    while resp.status not in ("completed", "failed"):
        if resp.status == "paused":
            print(f"\nPaused: {resp.checkpoint_label}")
            print("[A]pprove or [R]eject? ", end="")
            choice = input().strip().lower()
            if choice in ("", "a", "y"):
                resp = tool(action="approve")
            else:
                print("Feedback: ", end="")
                fb = input().strip() or "Rejected"
                resp = tool(action="reject", feedback=fb)
            continue

        if resp.status != "in_progress":
            break

        print(f"\n{'='*60}")
        print(f"STEP: {resp.step}")
        print(f"{'='*60}")
        print(f"\n{resp.instruction}")
        if resp.tool_name:
            print(f"\nTool: {resp.tool_name}")
            print(f"Params: {json.dumps(resp.tool_params, indent=2)}")
        if resp.tools:
            print(f"Available: {', '.join(sorted(resp.tools.keys()))}")

        print("\nEnter JSON result (empty line to finish, /skip, /quit, /abort):")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                return
            if line.strip() == "" and lines and lines[-1].strip() == "":
                break
            lines.append(line)

        text = "\n".join(lines).strip()
        if text == "/quit":
            break
        if text == "/abort":
            resp = tool(action="abort")
            break
        if text == "/skip":
            result = {}
        else:
            try:
                result = json.loads(text)
            except json.JSONDecodeError as e:
                print(f"Invalid JSON: {e}")
                continue

        tool.write_output_files(resp.step, result)
        resp = tool(action="submit", result=result)

    print()
    if resp.status == "completed":
        print(f"COMPLETED ({resp.steps_completed} steps)")
    elif resp.status == "failed":
        print(f"FAILED: {resp.error}")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
