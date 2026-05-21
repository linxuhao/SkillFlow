"""Step validation — executes validation specs against step outputs.

Each validation spec is::

    {files: ["*.json", "output.md"], tool: "json_schema", inline_schema: {...}}

The validator loads the tool, expands file globs, and calls the tool
for each matching file. Results are aggregated.

Tools can declare either ``file`` (singular, called per-file) or
``files`` (plural, called once with all matches).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skillflow.tool_loader import ToolLoader


class StepValidator:
    """Runs validation tool specs against step output files."""

    def __init__(self, tool_loader: "ToolLoader", workspace_root: Path):
        self._tool_loader = tool_loader
        self._workspace_root = Path(workspace_root)

    def validate(self, specs: list[dict]) -> dict:
        """Run all validation specs. Returns {passed: True} or {passed: False, errors: [...]}."""
        errors: list[dict] = []

        for spec in specs:
            file_patterns = spec.get("files", [])
            tool_name = spec.get("tool", "")
            if not tool_name:
                continue

            try:
                fn = self._tool_loader.load_fn(tool_name)
            except ImportError as e:
                errors.append({"tool": tool_name, "error": f"Tool not found: {e}"})
                continue

            sig = inspect.signature(fn)
            takes_singular = "file" in sig.parameters
            takes_plural = "files" in sig.parameters

            base_kwargs = {k: v for k, v in spec.items()
                          if k not in ("files", "file", "tool")}

            if takes_plural:
                # Batch tool (e.g. json_schema): pass all file patterns
                base_kwargs["files"] = file_patterns
                base_kwargs.setdefault("workspace_root", str(self._workspace_root))
                try:
                    result = fn(**base_kwargs)
                    self._add_errors(result, tool_name, errors)
                except Exception as e:
                    errors.append({"tool": tool_name, "files": file_patterns,
                                   "error": str(e)})

            elif takes_singular:
                # Per-file tool (e.g. syntax_lint): call once per match
                for pattern in file_patterns:
                    matches = (list(self._workspace_root.rglob(pattern))
                               if "*" in pattern
                               else [self._workspace_root / pattern])
                    for match_path in matches:
                        if not match_path.is_file():
                            continue
                        rel = str(match_path.relative_to(self._workspace_root))
                        kwargs = dict(base_kwargs)
                        kwargs["file"] = rel
                        kwargs.setdefault("workspace_root", str(self._workspace_root))
                        try:
                            result = fn(**kwargs)
                            self._add_errors(result, tool_name, errors)
                        except Exception as e:
                            errors.append({"tool": tool_name, "file": rel,
                                           "error": str(e)})

            else:
                base_kwargs["files"] = file_patterns
                base_kwargs.setdefault("workspace_root", str(self._workspace_root))
                try:
                    result = fn(**base_kwargs)
                    self._add_errors(result, tool_name, errors)
                except Exception as e:
                    errors.append({"tool": tool_name, "error": str(e)})

        if not errors:
            return {"passed": True}
        return {"passed": False, "errors": errors}

    @staticmethod
    def _add_errors(result, tool_name: str, errors: list):
        """Extract error entries from a tool result."""
        if not isinstance(result, dict):
            return
        passed = result.get("all_passed", result.get("passed",
                    result.get("verdict") == "passed"))
        if passed:
            return
        for r in result.get("results", []):
            if not r.get("passed", False):
                errors.append(r)
        if not result.get("results"):
            errors.append({
                "tool": tool_name,
                "error": result.get("feedback", result.get("error", str(result)))
            })
