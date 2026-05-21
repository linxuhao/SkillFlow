"""Tool implementation for skillflow_lint.

Loadable via ToolLoader as tool_name="skillflow_lint".
"""

from skillflow.plugins.linter import skillflow_lint as _skillflow_lint


def skillflow_lint(**kwargs) -> dict:
    """Validate a skillflow pipeline YAML file.

    Parameters:
        path: Path to the YAML file.
        content: Raw YAML string (alternative to path).

    Returns:
        {passed, errors, warnings, issues: [{severity, message, location, suggestion}]}
    """
    return _skillflow_lint(**kwargs)
