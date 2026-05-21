"""Tool implementation for stepflow_lint.

Loadable via ToolLoader as tool_name="stepflow_lint".
"""

from stepflow.plugins.linter import stepflow_lint as _stepflow_lint


def stepflow_lint(**kwargs) -> dict:
    """Validate a stepflow pipeline YAML file.

    Parameters:
        path: Path to the YAML file.
        content: Raw YAML string (alternative to path).

    Returns:
        {passed, errors, warnings, issues: [{severity, message, location, suggestion}]}
    """
    return _stepflow_lint(**kwargs)
