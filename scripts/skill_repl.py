#!/usr/bin/env python3
"""Interactive REPL for running a stepflow skill as a human agent.

Drives the pipeline manually with action="next"/"submit"/"approve"/"reject".

Usage:
    python3 scripts/skill_repl.py <graph.yaml>
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from stepflow.core import StepFlow
from stepflow.graph import PipelineGraph
from plugins.skill_runner import SkillTool
from plugins.linter import stepflow_lint
from tests.mocks import MockToolLoader, create_standard_mock_tools


def main():
    graph_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not graph_path:
        print("Usage: python3 scripts/skill_repl.py <graph.yaml>")
        sys.exit(1)

    tmp = tempfile.mkdtemp(prefix="stepflow_repl_")
    mt = MockToolLoader()
    mt.register("stepflow_lint", stepflow_lint)
    for name, fn in create_standard_mock_tools()._tools.items():
        mt.register(name, fn)

    sf = StepFlow(
        ":memory:",
        tool_loader=mt,
        workspace_base=os.path.join(tmp, "ws"),
        projects_base=os.path.join(tmp, "projects"),
    )

    graph = PipelineGraph.from_yaml(graph_path)

    # Register agent configs
    from plugins.skill_converter.converter import _register_converter_agents
    for step in graph.steps:
        if step.agent_config:
            try:
                sf.register_agent_config(step.agent_config, model="host")
            except Exception:
                pass
    if "skill_converter" in graph_path:
        _register_converter_agents(sf)

    sf.register_graph(graph)

    print(f"Pipeline: {graph.name} ({len(graph.steps)} steps)")
    print(f"Begin: {graph.begin}")
    print()

    tool = SkillTool(sf, graph.name)

    # ── Manual interactive loop ──────────────────────────────────
    resp = tool(action="next")

    while resp.status not in ("completed", "failed"):
        if resp.status == "paused":
            print(f"\n⏸  PAUSED: {resp.checkpoint_label}")
            print(f"   [A]pprove or [R]eject? ", end="")
            choice = input().strip().lower()
            if choice in ("", "a", "y"):
                resp = tool(action="approve")
            else:
                print("   Feedback: ", end="")
                fb = input().strip() or "Rejected"
                resp = tool(action="reject", feedback=fb)
            continue

        if resp.status != "in_progress":
            break

        print(f"\n{'='*60}")
        print(f"STEP: {resp.step}")
        print(f"{'='*60}")
        print(f"\n{resp.instruction}")
        if resp.tools:
            print(f"\nTools: {', '.join(sorted(resp.tools.keys()))}")

        print(f"\nEnter JSON result (empty line to finish, /skip, /quit):")
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
        print(f"✓ COMPLETED ({resp.steps_completed} steps)")
    elif resp.status == "failed":
        print(f"✗ FAILED: {resp.error}")

    os.system(f"rm -rf {tmp}")


if __name__ == "__main__":
    main()
