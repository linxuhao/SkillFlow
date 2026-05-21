"""Skill Converter — converts skill descriptions into stepflow pipeline configs.

Runs a fixed converter pipeline (skill_converter.yaml) where each agent
step is executed by the provided SkillTool (which delegates to the host LLM).
The linter's stepflow_lint tool provides the validation feedback loop.
"""

from plugins.skill_converter.converter import setup_converter, get_output_file, save_output

__all__ = ["setup_converter", "get_output_file", "save_output"]
