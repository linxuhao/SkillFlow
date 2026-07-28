"""An agent must be able to recover from its own bad tool call.

A tool that RETURNS ``{"error": ...}`` is fed back into the ReAct loop and is
survivable. A tool that RAISES used to propagate straight to a failed run, so an
agent that mistyped one argument name could never correct it — the mistake never
came back to it. Observed live: `read_file() missing 1 required positional
argument: 'path'` ended a whole pipeline test-drive at its second step.

The typo is not carelessness. The registry names "the file to operate on" six ways
(`path`, `file`, `files`, `filename`, `file_path`, `graph_path`) and the two most-used
tools disagree — `write(file=…)` vs `read_file(path=…)`.
"""
from unittest.mock import MagicMock

from skillflow.core import SkillFlow


def _sf_with(fn, name="read_file"):
    sf = SkillFlow(":memory:")
    loader = MagicMock()
    loader.load_fn.return_value = fn
    loader.load_schema.return_value = {"name": name}
    loader.list_tools.return_value = [name]
    sf._tool_loader = loader
    return sf


def test_a_raised_error_becomes_a_tool_result(monkeypatch):
    def read_file(path, workspace_root=""):
        return {"content": "x"}

    sf = _sf_with(read_file)
    # `size` is not a parameter and `path` is missing → the call would raise
    res = sf.execute_tool("read_file", {"nonsense_a": 1, "nonsense_b": 2})
    assert "error" in res, "a raised TypeError must not escape as an exception"
    assert "read_file() failed" in res["error"]
    assert "Accepted parameters: path" in res["error"]
    assert "nonsense_a" in res["error"] and "nonsense_b" in res["error"]


def test_the_single_wrong_name_is_rebound(monkeypatch):
    """`read_file(file=…)` is the exact mistake `write(file=…)` teaches."""
    seen = {}

    def read_file(path, workspace_root=""):
        seen["path"] = path
        return {"content": "hello"}

    sf = _sf_with(read_file)
    res = sf.execute_tool("read_file", {"file": "notes.md"})
    assert res == {"content": "hello"}
    assert seen["path"] == "notes.md"


def test_rebinding_does_not_guess_when_it_is_ambiguous():
    """Two dropped names and two holes is a guess, not an inference — report instead."""
    def two_required(alpha, beta, workspace_root=""):
        return {"ok": True}

    sf = _sf_with(two_required, name="two_required")
    res = sf.execute_tool("two_required", {"x": 1, "y": 2})
    assert "error" in res
    assert "Accepted parameters: alpha, beta" in res["error"]


def test_a_correct_call_is_untouched():
    def read_file(path, workspace_root=""):
        return {"content": path}

    sf = _sf_with(read_file)
    assert sf.execute_tool("read_file", {"path": "a.md"}) == {"content": "a.md"}


def test_an_optional_parameter_is_not_treated_as_a_hole():
    """Only REQUIRED parameters are candidates; an unfilled default is not missing."""
    seen = {}

    def pytest_tool(file, verbose=False, workspace_root=""):
        seen.update(file=file, verbose=verbose)
        return {"verdict": "passed"}

    sf = _sf_with(pytest_tool, name="pytest")
    res = sf.execute_tool("pytest", {"path": "t.py"})
    assert res == {"verdict": "passed"}
    assert seen == {"file": "t.py", "verbose": False}


def test_a_tool_that_raises_for_a_real_reason_still_reports_cleanly():
    def exploding(path, workspace_root=""):
        raise RuntimeError("disk on fire")

    sf = _sf_with(exploding, name="exploding")
    res = sf.execute_tool("exploding", {"path": "a"})
    assert res["error"].startswith("exploding() failed: RuntimeError: disk on fire")


def test_framework_injected_params_are_not_advertised():
    """`workspace_root` / `run_id` etc. are injected by the engine — telling the
    agent to pass them invites it to try."""
    def read_file(path, workspace_root="", project_root="", step_id="", run_id=""):
        raise ValueError("boom")

    sf = _sf_with(read_file)
    res = sf.execute_tool("read_file", {"path": "a"})
    assert "Accepted parameters: path" in res["error"]
    for injected in ("workspace_root", "project_root", "step_id", "run_id"):
        assert injected not in res["error"].split("Accepted parameters:")[1]
