"""Mock StepRunner and ToolLoader for integration tests.

These mocks let tests drive skillflow pipelines without real LLM calls
or actual tool implementations. The StepRunner returns canned responses
keyed by step_id; the ToolLoader provides duck-typed tool functions
that return static success/failure dicts.
"""

from __future__ import annotations

from typing import Any, Callable

from skillflow.core import ClaimedStep, StepResult


class MockStepRunner:
    """StepRunner that returns canned StepResults per step_id.

    Usage::

        runner = MockStepRunner({
            "research": {"outputs": {"sota": "..."}, "flags": {}},
            "review": {"outputs": {"verdict": {"passed": True}}, "flags": {}},
        })
        result = await runner.execute(claimed_step)
    """

    def __init__(self, responses: dict[str, dict] | None = None):
        self.responses: dict[str, dict] = dict(responses or {})
        self.call_log: list[ClaimedStep] = []

    async def execute(self, step: ClaimedStep) -> StepResult:
        self.call_log.append(step)
        canned = self.responses.get(step.step_id, {})
        return StepResult(
            outputs=canned.get("outputs", {}),
            flags=canned.get("flags", {}),
        )


class MockToolLoader:
    """Duck-typed ToolLoader — maps tool names to callables.

    SkillFlow calls ``load_fn(name)`` and ``load_schema(name)`` on the
    tool_loader object.  This mock satisfies both without touching the
    filesystem.

    Usage::

        tools = MockToolLoader()
        tools.register("file_exists", lambda **kw: {"passed": True})
        sf = SkillFlow(":memory:", tool_loader=tools)
    """

    def __init__(self, tools: dict[str, Callable] | None = None):
        self._tools: dict[str, Callable] = dict(tools or {})
        self._schemas: dict[str, dict] = {}
        self._custom_names: set[str] = set()

    def register(self, name: str, fn: Callable, schema: dict | None = None,
                 *, native: bool = True):
        self._tools[name] = fn
        if schema:
            self._schemas[name] = schema
        if not native:
            self._custom_names.add(name)

    def is_native(self, name: str) -> bool:
        return name not in self._custom_names

    def load_fn(self, name: str) -> Callable:
        if name not in self._tools:
            raise ImportError(f"Mock tool '{name}' not found")
        return self._tools[name]

    def load_schema(self, name: str) -> dict:
        if name in self._schemas:
            return self._schemas[name]
        return {"name": name, "description": f"Mock {name}"}

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())


# ── Built-in mock tool functions ──────────────────────────────────────

def _mock_pass(**kwargs) -> dict:
    return {"passed": True}


def _mock_fail(**kwargs) -> dict:
    return {"passed": False, "error": "mock failure"}


def _mock_file_exists(**kwargs) -> dict:
    return {"passed": True}


def _mock_json_schema(**kwargs) -> dict:
    return {"passed": True}


def _mock_syntax_lint(**kwargs) -> dict:
    return {"passed": True}


def _mock_py_compile(**kwargs) -> dict:
    return {"passed": True}


def _mock_pytest(**kwargs) -> dict:
    return {"passed": True}


def _mock_repo_apply(**kwargs) -> dict:
    return {"passed": True}


def _mock_dir_tree(**kwargs) -> dict:
    return {"tree": "mock/", "files": []}


# Dict of all standard tools used in DPE config
STANDARD_MOCK_TOOLS: dict[str, Callable] = {
    "file_exists": _mock_file_exists,
    "json_schema": _mock_json_schema,
    "syntax_lint": _mock_syntax_lint,
    "py_compile": _mock_py_compile,
    "pytest": _mock_pytest,
    "repo_apply": _mock_repo_apply,
    "dir_tree": _mock_dir_tree,
}


def create_standard_mock_tools() -> MockToolLoader:
    """Return a MockToolLoader pre-loaded with standard DPE tools."""
    return MockToolLoader(dict(STANDARD_MOCK_TOOLS))
