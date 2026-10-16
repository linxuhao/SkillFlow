"""Actual engine validation dispatch carries identity without ambient state."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import skillflow
from skillflow import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.lint_backends import register_backend, _backends
from skillflow.step_validation import StepValidator
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager

TOOLS = Path(skillflow.__file__).parent / "tools"

@pytest.fixture(autouse=True)
def restore_registry():
    old = dict(_backends)
    yield
    _backends.clear()
    _backends.update(old)


def engine(tmp_path, specs, target="code"):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "context-test"],
                 ["config", "user.email", "context@test.invalid"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "baseline").write_text("baseline")
    subprocess.run(["git", "-C", str(repo), "add", "baseline"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
    sf = SkillFlow(":memory:")
    sf._tool_loader = ToolLoader(TOOLS)
    sf._workspace = WorkspaceManager(str(tmp_path / "artifacts"),
        code_path_resolver=lambda project_id, run_id=None: repo)
    node = StepNode(id="implement", output_mode="write", output_target=target,
        output_allow_full_write=True, validation=specs, transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name="context", begin=node.id, steps=[node]))
    rid = sf.create_run("context", {"project_id": "project"}, project_id="project")
    sf.start_run(rid)
    sf.advance_run(rid)
    claim = sf.claim_next_step(rid)
    (repo / "linter_manifest.json").write_text('{".js":"owned_js"}')
    return sf, repo, node, claim


def owned_backend(expected, seen):
    def backend(fp, *, run_id="", step_id="", project_root=""):
        got = (run_id, step_id, project_root)
        seen.append(got)
        root = Path(project_root) if project_root else None
        if got != expected or root is None or root not in fp.resolve().parents:
            return {"passed": False, "error_message": "owned lint refused missing/wrong context"}
        node = "/opt/aitelier-node/bin/node" if Path("/opt/aitelier-node/bin/node").exists() else shutil.which("node")
        assert node, "real Node parser required for this integration fixture"
        done = subprocess.run([node, "--check", str(fp)], capture_output=True, text=True, timeout=10)
        return {"passed": done.returncode == 0,
                "error_message": done.stdout + done.stderr}
    return backend


@pytest.mark.parametrize("callsite", ["outputs", "legacy_after_deliver"])
@pytest.mark.parametrize("valid", [True, False])
def test_real_engine_native_lint_backend_has_authoritative_context(tmp_path, callsite, valid):
    specs = [{"tool": "lint", "files": ["candidate.js"], "manifest_path": "linter_manifest.json",
              "run_id": "foreign-run", "step_id": "foreign-step", "project_root": "/outside"}]
    sf, repo, node, claim = engine(tmp_path, specs, "code" if callsite == "outputs" else "artifact")
    (repo / "candidate.js").write_text("const answer = 42;" if valid else "function (( {")
    if callsite == "outputs":
        sf._code_output(claim.token.run_id, node.id).record(["candidate.js"])
    seen = []
    expected = (claim.token.run_id, node.id, str(repo))
    register_backend("owned_js", owned_backend(expected, seen))
    if callsite == "outputs":
        result = sf._validate_outputs(claim.token, node)
    else:
        result = sf._execute_check_hook(claim.token, node, "after_deliver", specs)
    assert seen == [expected], result
    assert result["passed"] is valid, result
    if not valid:
        assert "SyntaxError" in str(result)


@pytest.mark.parametrize("context", [None, {"run_id": "wrong", "step_id": "s", "project_root": "/outside"}])
def test_absent_or_wrong_host_context_cannot_be_supplied_by_spec(tmp_path, context):
    (tmp_path / "candidate.js").write_text("const answer=42;")
    (tmp_path / "manifest.json").write_text('{".js":"owned_js"}')
    seen = []
    register_backend("owned_js", owned_backend(("own", "s", str(tmp_path)), seen))
    kwargs = {} if context is None else {"tool_context": context}
    result = StepValidator(ToolLoader(TOOLS), tmp_path, **kwargs).validate([
        {"tool": "lint", "files": ["candidate.js"], "manifest_path": "manifest.json",
         "run_id": "own", "step_id": "s", "project_root": str(tmp_path)}])
    assert result["passed"] is False
    assert "refused" in str(result)
    expected = [("", "", "")] if context is None else [("wrong", "s", "/outside")]
    assert seen == expected


def test_path_only_backend_keeps_existing_public_convention(tmp_path):
    (tmp_path / "candidate.js").write_text("const answer=42;")
    (tmp_path / "manifest.json").write_text('{".js":"old_path_only"}')
    seen = []
    def legacy(fp):
        seen.append(fp)
        return {"passed": True, "error_message": ""}
    register_backend("old_path_only", legacy)
    result = StepValidator(ToolLoader(TOOLS), tmp_path,
        tool_context={"run_id": "own", "step_id": "s", "project_root": str(tmp_path)}).validate([
        {"tool": "lint", "files": ["candidate.js"], "manifest_path": "manifest.json"}])
    assert result["passed"] is True
    assert seen == [tmp_path / "candidate.js"]


def test_context_is_copied_and_only_supported_explicit_keywords_are_forwarded(tmp_path):
    (tmp_path / "candidate.js").write_text("const answer=42;")
    (tmp_path / "manifest.json").write_text('{".js":"run_only"}')
    seen = []
    def backend(fp, *, run_id=""):
        seen.append((fp, run_id))
        return {"passed": run_id == "own", "error_message": ""}
    register_backend("run_only", backend)
    context = {"run_id": "own", "step_id": "s", "project_root": str(tmp_path)}
    validator = StepValidator(ToolLoader(TOOLS), tmp_path, tool_context=context)
    context["run_id"] = "changed"
    result = validator.validate([{ "tool": "lint", "files": ["candidate.js"], "manifest_path": "manifest.json"}])
    assert result["passed"] is True
    assert seen == [(tmp_path / "candidate.js", "own")]
