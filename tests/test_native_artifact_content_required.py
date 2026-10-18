"""Required content is enforced by the actual generated-tool dispatcher."""
from pathlib import Path

import pytest
import skillflow
from skillflow import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader


def engine(tmp_path, *, mode="replace", generic=False):
    sf = SkillFlow(str(tmp_path / "engine.db"),
                   workspace_base=str(tmp_path / "workspace"),
                   tool_loader=ToolLoader(Path(skillflow.__file__).parent / "tools"))
    node = StepNode(id="investigate", output_mode="write" if generic else "content",
                    output_allow_full_write=generic,
                    output_fixed={} if generic else {
                        "findings": {"file": "findings.md", "on_exists": mode}},
                    transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name="content", begin=node.id, steps=[node]))
    rid = sf.create_run("content", project_id="owned")
    sf.start_run(rid)
    claim = sf.claim_next_step(rid)
    return sf, rid, claim, Path(claim.inputs["_output_dir"])


def call(sf, rid, claim, name, params):
    return sf.execute_tool(name, params, run_id=rid, step_id=claim.step_id,
                           step_instance_id=claim.token.step_instance_id,
                           claim_epoch=claim.token.claim_epoch)


@pytest.mark.parametrize("name,mode", [
    ("create_findings", "replace"), ("write_findings", "replace"),
    ("write_findings", "new"), ("write_findings", "append"),
    ("write", "replace"),
])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("missing", [{}, {"content": None}, {"text": "wrong argument"}])
def test_missing_content_refuses_without_file_or_archive_changes(
        tmp_path, name, mode, existing, missing):
    sf, rid, claim, directory = engine(tmp_path, mode=mode, generic=name == "write")
    assert name in claim.inputs["_tool_schemas"]
    if existing:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "findings.md").write_bytes(b"KEEP existing content\r\n")
    before = {p.name: p.read_bytes() for p in directory.glob("*") if p.is_file()}
    params = {"file": "findings.md", **missing} if name == "write" else missing
    result = call(sf, rid, claim, name, params)
    assert "content" in result.get("error", "").lower(), result
    assert not any(k in result for k in ("written", "archived", "edited")), result
    assert {p.name: p.read_bytes() for p in directory.glob("*") if p.is_file()} == before
    assert sf.get_steps(rid)[0]["status"] == "claimed"
    assert sf.unsettled_operations() == []


def test_refusal_corrects_then_edits_and_finishes_in_the_same_claim(tmp_path):
    sf, rid, claim, directory = engine(tmp_path)
    assert "error" in call(sf, rid, claim, "create_findings", {})
    created = call(sf, rid, claim, "create_findings", {"initialContent": "useful BEFORE\n"})
    assert created["written"] == "findings.md" and "error" not in created
    rewritten = call(sf, rid, claim, "write_findings", {"content": "useful AFTER\n"})
    assert rewritten["written"] == "findings.md" and "error" not in rewritten
    assert call(sf, rid, claim, "write_findings", {"content": "useful AFTER\n"}) == rewritten
    assert (directory / "findings.md").read_text() == "useful AFTER\n"
    edited = call(sf, rid, claim, "edit_findings", {"old_str": "AFTER", "new_str": "FINAL"})
    assert edited["edited"] == "findings.md" and "error" not in edited
    assert (directory / "findings.md").read_text() == "useful FINAL\n"
    assert call(sf, rid, claim, "finish_step", {"summary": "useful artifact"})["status"] == "completed"
    sf.confirm_step(claim.token, StepResult())
    sf.advance_run(rid)
    assert sf.get_run(rid)["status"] == "completed"
    published = sf._workspace.get_step_dir("owned", "content", "investigate") / "findings.md"
    assert published.read_text() == "useful FINAL\n"
    assert sf.unsettled_operations() == []


@pytest.mark.parametrize("name,key", [("create_findings", "initialContent"),
                                      ("write_findings", "content")])
def test_explicit_empty_fixed_artifact_remains_supported(tmp_path, name, key):
    sf, rid, claim, directory = engine(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "findings.md").write_text("existing\n")
    result = call(sf, rid, claim, name, {key: ""})
    assert "error" not in result and result["written"] == "findings.md"
    assert (directory / "findings.md").read_bytes() == b""
    if name.startswith("create"):
        assert (directory / result["archived"]).read_text() == "existing\n"
