#!/usr/bin/env python3
"""Interactive converter — turn a skill description into a stepflow pipeline.

Drives the skill_converter meta-pipeline interactively. The user (or LLM
agent) responds to each prompt with JSON. On completion, the generated
pipeline YAML is saved to the output path.

Usage:
    stepflow-convert my_skill.md -o pipeline.yaml
    stepflow-convert -d "Code review skill..." -o pipeline.yaml
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

from stepflow.core import StepFlow
from stepflow.tool_loader import ToolLoader
from stepflow.plugins.skill_runner import SkillTool
from stepflow.plugins.skill_converter import setup_converter, save_output


def main():
    args = sys.argv[1:]
    desc_file = None
    desc_text = ""
    output = "pipeline.yaml"

    i = 0
    while i < len(args):
        if args[i] == "-d" and i + 1 < len(args):
            i += 1
            desc_text = args[i]
        elif args[i] == "-o" and i + 1 < len(args):
            i += 1
            output = args[i]
        elif not args[i].startswith("-") and desc_file is None:
            desc_file = args[i]
        i += 1

    if desc_file:
        desc_text = Path(desc_file).read_text(encoding="utf-8")
    elif not desc_text:
        print("Usage: stepflow-convert <description.md> [-o pipeline.yaml]")
        print("       stepflow-convert -d 'skill description...' [-o pipeline.yaml]")
        sys.exit(1)

    tmp = tempfile.mkdtemp(prefix="stepflow_convert_")

    # Load tools
    loader = ToolLoader()
    import stepflow.plugins
    loader.add_tools_dir(str(Path(stepflow.plugins.__path__[0]) / "linter" / "tools"))

    sf = StepFlow(
        ":memory:",
        tool_loader=loader,
        delegate_tools_to_agent=True,
        workspace_base=os.path.join(tmp, "ws"),
        projects_base=os.path.join(tmp, "projects"),
    )

    tool = setup_converter(sf, description=desc_text)

    print(f"Skill description: {len(desc_text)} chars")
    print(f"Output: {output}")
    print()

    # Interactive loop
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
        saved = save_output(sf, tool.run_id, output)
        print(f"COMPLETED — pipeline saved to {saved}")
    elif resp.status == "failed":
        print(f"FAILED: {resp.error}")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
