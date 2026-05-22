#!/usr/bin/env python3
"""Stateless converter wrapper — calls skillflow-run with the fixed converter pipeline.

Usage:
    skillflow-convert --desc "Code review skill..." --action next
    skillflow-convert --desc-file my_skill.md --action next
    skillflow-convert --action submit --run-id <id> --result '{"analysis": {...}}'
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_CONVERTER_YAML = _REPO_ROOT / "src" / "skillflow" / "plugins" / "skill_converter" / "skill_converter.yaml"
_WORKSPACE = os.path.expanduser("~/.skillflow/workspaces")
_PROJECT_ID = "skill-converter"


def main():
    parser = argparse.ArgumentParser(
        description="Stateless skill-to-pipeline converter — wraps skillflow-run "
                    "with the built-in skill_converter pipeline.",
        epilog=(
            "Examples:\n"
            "  skillflow-convert --desc \"Code review skill...\" --action next\n"
            "  skillflow-convert --desc-file my_skill.md --action next\n"
            "  skillflow-convert --action submit --run-id <id> --result '{\"analysis\":{...}}'\n"
            "  skillflow-convert --action next --run-id <id>       # reconnect\n"
            "\n"
            "Workflow:\n"
            "  1. Start:    --desc \"...\" --action next            → returns JSON with run_id + step\n"
            "  2. Submit:   --action submit --run-id <id> --result '{...}'\n"
            "  3. Continue as with skillflow-run for remaining steps.\n"
            "\n"
            "On completion, the generated pipeline YAML is at:\n"
            "  ~/.skillflow/workspaces/skill-converter/skill_converter/design_graph/skill_pipeline.yaml\n"
            "\n"
            "This is a thin wrapper — it writes the skill description to the workspace\n"
            "then calls skillflow-run with the fixed converter pipeline graph."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--desc", default="", help="Skill description text (required on first call)")
    parser.add_argument("--desc-file", default="", help="Path to skill description markdown file")
    parser.add_argument("--action", default="next",
                        choices=["next", "submit", "approve", "reject", "abort"],
                        help="Action: next (start/advance), submit (confirm step)")
    parser.add_argument("--run-id", default="", help="Run ID from previous JSON response")
    parser.add_argument("--step-id", default="", help="Step ID from response")
    parser.add_argument("--result", default="{}", help="Result JSON for submit")
    parser.add_argument("--feedback", default="", help="Feedback message for reject")
    args = parser.parse_args()

    # Validate required argument combinations
    if args.action in ("submit", "approve", "reject", "abort") and not args.run_id:
        parser.error(f"--action {args.action} requires --run-id")
    if args.action == "next" and not args.run_id and not args.desc and not args.desc_file:
        parser.error("--action next requires --desc/--desc-file (first call) or --run-id (resume)")

    # On first call (no run-id), write the skill description to workspace
    if args.action == "next" and not args.run_id:
        desc_text = args.desc
        if args.desc_file:
            desc_text = Path(args.desc_file).read_text(encoding="utf-8")
        if not desc_text.strip():
            print(json.dumps({"status": "failed", "error": "No skill description provided (--desc or --desc-file)"}))
            sys.exit(1)

        desc_dir = Path(_WORKSPACE) / _PROJECT_ID
        desc_dir.mkdir(parents=True, exist_ok=True)
        (desc_dir / "skill_description.md").write_text(desc_text, encoding="utf-8")

    # Build skillflow-run command
    runner = str(_REPO_ROOT / "scripts" / "skill_run.py")
    cmd = [
        sys.executable, runner,
        "--graph", str(_CONVERTER_YAML),
        "--project-id", _PROJECT_ID,
        "--delegate-tools",
        "--action", args.action,
    ]
    if args.run_id:
        cmd.extend(["--run-id", args.run_id])
    if args.step_id:
        cmd.extend(["--step-id", args.step_id])
    if args.result:
        cmd.extend(["--result", args.result])
    if args.feedback:
        cmd.extend(["--feedback", args.feedback])

    # Ensure PYTHONPATH includes src/
    env = os.environ.copy()
    src_path = str(_REPO_ROOT / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src_path}:{existing}" if existing else src_path

    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
