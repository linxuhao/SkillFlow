"""Converter — sets up the skill_converter pipeline for manual interactive use.

Usage::

    from stepflow.plugins.skill_converter import setup_converter, extract_pipeline

    # Setup: registers converter graph, writes skill description to workspace
    tool = setup_converter(sf, description="# Code Review Skill\\n...")

    # Drive manually with run_skill (the agent calls these interactively):
    resp = tool(action="next")
    # ... agent does work ...
    resp = tool(action="submit", result={"analysis": {...}})
    # ... agent does work ...
    resp = tool(action="submit", result={"pipeline": "..."})
    # ... possibly fix_issues if linter found errors ...
    # resp.status == "completed"

    # Get the output file (written by the agent during the session):
    path = get_output_file(sf, tool.run_id)
    graph = PipelineGraph.from_yaml(str(path))
"""

from __future__ import annotations

from pathlib import Path

from stepflow.core import StepFlow
from stepflow.graph import PipelineGraph
from stepflow.plugins.skill_runner.runner import SkillTool


_CONVERTER_DIR = Path(__file__).parent
_CONVERTER_YAML = _CONVERTER_DIR / "skill_converter.yaml"


def setup_converter(
    sf: StepFlow,
    description: str = "",
    *,
    description_file: str | Path | None = None,
    project_id: str = "skill-converter",
) -> SkillTool:
    """Prepare the converter pipeline for interactive use.

    Registers the converter graph and agent configs, writes the skill
    description to the workspace, and returns a SkillTool pointing at
    the ``skill_converter`` graph ready for ``action="next"``.

    Args:
        sf: StepFlow instance (must have workspace + stepflow_lint tool).
        description: The skill description as a string.
        description_file: Path to a markdown file containing the
            skill description.  Takes precedence over ``description``.
        project_id: Project ID for the converter run.

    Returns:
        A SkillTool ready for ``tool(action="next")``.
    """
    # Resolve the description text
    if description_file:
        desc_text = Path(description_file).read_text(encoding="utf-8")
    else:
        desc_text = description

    if not desc_text.strip():
        raise ValueError("No skill description provided")

    # Register agent configs used by the converter pipeline
    _register_converter_agents(sf)

    # Register the converter graph (idempotent)
    try:
        converter_graph = PipelineGraph.from_yaml(str(_CONVERTER_YAML))
        sf.register_graph(converter_graph)
    except Exception:
        pass  # Already registered

    # Write the skill description to the project workspace
    sf.create_project(project_id, name="skill-converter")
    _write_description(sf, project_id, desc_text)

    tool = SkillTool(sf, "skill_converter", project_id=project_id)
    return tool


def get_output_file(sf: StepFlow, run_id: str) -> Path | None:
    """Return the path to the generated pipeline YAML in the workspace.

    Checks fix_issues/ first (post-fix output), then design_graph/
    (first-attempt output).

    Args:
        sf: The StepFlow instance used for the converter run.
        run_id: The converter run ID (from ``tool.run_id``).

    Returns:
        Path to the YAML file in the workspace, or None if not found.
    """
    pid = sf._get_project_id(run_id)
    gname = sf._get_graph_name(run_id)
    if not pid or not gname or not sf._workspace:
        return None

    for step_id in ("fix_issues", "design_graph"):
        f = sf._workspace.get_step_dir(pid, gname, step_id) / "skill_pipeline.yaml"
        if f.exists():
            return f
    return None


def save_output(sf: StepFlow, run_id: str,
                output_file: str | Path) -> Path:
    """Copy the generated pipeline YAML from the workspace to a destination.

    Call this after the converter completes (resp.status == "completed").

    Args:
        sf: The StepFlow instance used for the converter run.
        run_id: The converter run ID.
        output_file: Destination path (e.g. "skills/review/pipeline.yaml").

    Returns:
        Path to the destination file.

    Raises:
        RuntimeError: No generated YAML found in the workspace.
    """
    src = get_output_file(sf, run_id)
    if src is None:
        raise RuntimeError("No generated pipeline found in workspace")

    import shutil
    dest = Path(output_file)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


# ── Internal ──────────────────────────────────────────────────────────

def _register_converter_agents(sf: StepFlow):
    """Register the agent configs needed by the converter pipeline."""
    agents = {
        "skill_analyst": {
            "model": "host",
            "tools": ["read_file", "write"],
            "system_prompt": _load_prompt("analyze_skill.md"),
        },
        "graph_designer": {
            "model": "host",
            "tools": ["read_file", "write"],
            "system_prompt": _load_prompt("design_graph.md"),
        },
        "graph_fixer": {
            "model": "host",
            "tools": ["read_file", "write"],
            "system_prompt": _load_prompt("fix_issues.md"),
        },
    }
    for name, cfg in agents.items():
        try:
            sf.register_agent_config_from_dict(name, cfg)
        except Exception:
            pass


def _load_prompt(filename: str) -> str:
    path = _CONVERTER_DIR / "prompts" / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _write_description(sf: StepFlow, project_id: str, description: str):
    """Write the skill description into the project workspace."""
    if not sf._workspace:
        return
    ws_dir = sf._workspace.get_project_path(project_id)
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "skill_description.md").write_text(description, encoding="utf-8")
