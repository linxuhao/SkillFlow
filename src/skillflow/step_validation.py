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


def _accepts(fn, param: str) -> bool:
    """True if `fn` declares `param` or takes `**kwargs`."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    if param in sig.parameters:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD
               for p in sig.parameters.values())


class StepValidator:
    """Runs validation tool specs against step output files."""

    def __init__(self, tool_loader: "ToolLoader", workspace_root: Path,
                 trace_sink=None, config_name: str = ""):
        self._tool_loader = tool_loader
        self._workspace_root = Path(workspace_root)
        # Which pipeline this validation belongs to. A validation tool that has
        # to consult per-pipeline state (e.g. "is this capability one this
        # pipeline offers?") otherwise receives an empty name and evaluates
        # against nothing — passing or failing for a reason unrelated to the
        # files it was handed.
        self._config_name = config_name
        # Optional callable(event: str, payload: dict) — pre-bound by the caller
        # with run/step ids so validation/check tools land in the run trace too.
        self._trace_sink = trace_sink

    def _trace(self, event: str, payload: dict) -> None:
        if self._trace_sink:
            try:
                self._trace_sink(event, payload)
            except Exception:
                pass

    def validate(self, specs: list[dict]) -> dict:
        """Run all validation specs.

        Each spec may include ``on_failure: "warn"`` — failures from that
        spec are collected as warnings (non-fatal) rather than errors.

        Returns::

            {
                "passed": bool,       # fatal errors only
                "errors": [...],      # fatal failures
                "warnings": [...]     # warn-level failures (on_failure="warn")
            }
        """
        errors: list[dict] = []
        warnings: list[dict] = []

        for spec in specs:
            file_patterns = spec.get("files", [])
            tool_name = spec.get("tool", "")
            on_failure = spec.get("on_failure", "fail")
            if not tool_name:
                continue

            try:
                fn = self._tool_loader.load_fn(tool_name)
            except ImportError as e:
                errors.append({"tool": tool_name, "error": f"Tool not found: {e}"})
                self._trace(tool_name, {"source": "validation", "error": f"not found: {e}"})
                continue
            self._trace(tool_name, {"source": "validation", "files": file_patterns})

            sig = inspect.signature(fn)
            takes_singular = "file" in sig.parameters
            takes_plural = "files" in sig.parameters

            base_kwargs = {k: v for k, v in spec.items()
                          if k not in ("files", "file", "tool", "on_failure", "max_retries")}
            # `config_name` is offered, not imposed: most validation tools take a
            # fixed signature (`file_exists(files, *, workspace_root="")` and
            # friends have no **kwargs), and handing them an argument they never
            # declared turns EVERY validation spec into
            # "file_exists() got an unexpected keyword argument" — a step that
            # then burns its whole retry budget and fail-opens. ContextResolver
            # already filters this way five files over; the same rule belongs
            # here.
            if self._config_name and _accepts(fn, "config_name"):
                base_kwargs.setdefault("config_name", self._config_name)

            if takes_plural:
                # Batch tool (e.g. json_schema): pass all file patterns
                base_kwargs["files"] = file_patterns
                base_kwargs.setdefault("workspace_root", str(self._workspace_root))
                try:
                    result = fn(**base_kwargs)
                    self._add_issues(result, tool_name, on_failure, errors, warnings)
                except Exception as e:
                    (warnings if on_failure == "warn" else errors).append(
                        {"tool": tool_name, "files": file_patterns,
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
                            self._add_issues(result, tool_name, on_failure, errors, warnings)
                        except Exception as e:
                            (warnings if on_failure == "warn" else errors).append(
                                {"tool": tool_name, "file": rel,
                                 "error": str(e)})

            else:
                base_kwargs["files"] = file_patterns
                base_kwargs.setdefault("workspace_root", str(self._workspace_root))
                try:
                    result = fn(**base_kwargs)
                    self._add_issues(result, tool_name, on_failure, errors, warnings)
                except Exception as e:
                    (warnings if on_failure == "warn" else errors).append(
                        {"tool": tool_name, "error": str(e)})

        return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}

    @staticmethod
    def _add_issues(result, tool_name: str, on_failure: str,
                    errors: list, warnings: list):
        """Extract issues from a tool result, routing to errors or warnings."""
        if not isinstance(result, dict):
            return
        passed = result.get("all_passed", result.get("passed",
                    result.get("verdict") == "passed"))
        if passed:
            return
        target = warnings if on_failure == "warn" else errors
        for r in result.get("results", []):
            if not r.get("passed", False):
                target.append(r)
        if not result.get("results"):
            target.append({
                "tool": tool_name,
                "error": result.get("feedback", result.get("error", str(result)))
            })
