"""Skill Runner — stateless CLI facade for LLM agents to execute skillflow pipelines.

The agent calls ``skillflow-run --action start --graph pipeline.yaml`` to begin,
does the work, then calls ``skillflow-run --action submit --run-id <id> --result '...'``
to hand in output. Each call is a fresh process — state lives in SQLite.
Each instruction opens with a whole-pipeline overview for planning, but
skillflow alone decides what runs next — gates, loops, checkpoints and error
routing stay behind the tool facade.
"""

from pathlib import Path

from skillflow.plugins.skill_runner.runner import SkillTool, SkillResponse, PromptAssembler
from skillflow.plugins.skill_runner.service import RunnerService


def load_agent_guide() -> str:
    """Return the AGENT.md content — usage guide for LLM agents using skillflow-run CLI."""
    return (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")


__all__ = ["SkillTool", "SkillResponse", "PromptAssembler", "RunnerService",
           "load_agent_guide"]
