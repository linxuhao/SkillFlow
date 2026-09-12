"""Revision boundaries: ownership, item routing, merge safety and restart."""
import json
from pathlib import Path

import pytest
import skillflow
from skillflow.artifact_revision import merge_candidate
from skillflow.core import SkillFlow, StepResult
from skillflow.exceptions import SkillFlowError
from skillflow.graph import PipelineGraph
from skillflow.tool_loader import ToolLoader
from test_artifact_revision import engine, call, write_set, revise, files


def test_addition_preserves_unmodified_cards(tmp_path):
    sf, rid, claim = engine(tmp_path)
    write_set(sf, rid, claim)
    claim = revise(sf, rid, claim)
    before = files(Path(claim.inputs["_artifact_dir"]))
    assert "error" not in call(sf, rid, claim, "write_card", id="D", content='{"id":"D"}')
    assert "error" not in call(sf, rid, claim, "write_manifest", content='{"execution_order":[["A","B","C","D"]]}')
    sf.confirm_step(claim.token, StepResult())
    after = files(Path(claim.inputs["_artifact_dir"]))
    for name in ("A", "B", "C"):
        assert after[f"tasks/{name}.json"] == before[f"tasks/{name}.json"]
    assert after["tasks/D.json"] == b'{"id":"D"}'


def test_partial_initialization_merge_preserves_current_bytes(tmp_path):
    baseline, current = tmp_path / "baseline", tmp_path / "current"
    baseline.mkdir(); current.mkdir()
    (baseline / "A.bin").write_bytes(b"old")
    (baseline / "B.bin").write_bytes(b"\x00\xff\x80")
    (current / "A.bin").write_bytes(b"changed")
    (current / "new.txt").write_text("new")
    assert merge_candidate(current, baseline) == ["B.bin"]
    assert files(current) == {"A.bin": b"changed", "B.bin": b"\x00\xff\x80", "new.txt": b"new"}


@pytest.mark.parametrize("location", ["baseline", "candidate"])
def test_candidate_merge_rejects_symlink_without_touching_outside(tmp_path, location):
    baseline, candidate, outside = tmp_path / "base", tmp_path / "candidate", tmp_path / "outside"
    for root in (baseline, candidate, outside): root.mkdir()
    (outside / "secret").write_text("untouched")
    target = baseline if location == "baseline" else candidate
    (target / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError): merge_candidate(candidate, baseline)
    assert (outside / "secret").read_text() == "untouched"


def test_another_run_cannot_take_a_pending_candidates_directory(tmp_path):
    sf, rid, claim = engine(tmp_path)
    write_set(sf, rid, claim)
    before = files(Path(claim.inputs["_output_dir"]))
    sf.release_claim(claim.token, "temporary host interruption")
    other = sf.create_run("revision", project_id="p")
    sf.start_run(other); sf.advance_run(other)
    with pytest.raises(SkillFlowError, match="owned"):
        sf.claim_next_step(other)
    assert files(Path(claim.inputs["_output_dir"])) == before
    assert sf.claim_next_step(rid).token.step_instance_id == claim.token.step_instance_id


def test_database_reopen_retains_deleted_card_and_new_file(tmp_path):
    sf, rid, claim = engine(tmp_path)
    write_set(sf, rid, claim); claim = revise(sf, rid, claim)
    assert call(sf, rid, claim, "delete_card", id="B").get("deleted")
    assert "error" not in call(sf, rid, claim, "write_card", id="D", content="{}")
    sf.release_claim(claim.token, "restart")
    restored = SkillFlow(str(tmp_path / "state.db"), workspace_base=str(tmp_path / "artifacts"),
        code_path_resolver=lambda pid, run_id=None: tmp_path / "repo",
        tool_loader=ToolLoader(Path(skillflow.__file__).parent / "tools"))
    claim = restored.claim_next_step(rid)
    current = Path(claim.inputs["_output_dir"])
    assert not (current / "tasks/B.json").exists()
    assert (current / "tasks/D.json").read_text() == "{}"
    assert "error" in call(restored, rid, claim, "read", source="self", path="tasks/B.json")


def test_loop_revision_keeps_own_item_and_never_copies_siblings(tmp_path):
    sf = SkillFlow(str(tmp_path / "loop.db"), workspace_base=str(tmp_path / "artifacts"))
    sf.register_graph(PipelineGraph._from_dict({"name": "loop_revision", "begin": "plan", "steps": [
        {"id": "plan", "step_type": "agent", "output": {"mode": "write"}, "transitions": [{"to": "loop"}]},
        {"id": "loop", "step_type": "loop", "loop": {
            "source": {"step": "plan", "file": "items.json", "field": "items"},
            "item_as": "task", "max_iterations": 5},
         "transitions": [{"to": "maker", "max_loop": 10}, {"to": "done"}]},
        {"id": "maker", "step_type": "agent", "output": {"mode": "write", "carry_forward": True},
         "transitions": [{"to": "review", "max_loop": 10}]},
        {"id": "review", "step_type": "agent", "output": {"mode": "write"},
         "transitions": [{"to": "maker", "match": {"passed": False}, "max_loop": 10},
                         {"to": "loop", "max_loop": 10}]},
        {"id": "done", "step_type": "gate", "transitions": [{"to": None}]}
    ]}))
    rid = sf.create_run("loop_revision", project_id="p")
    sf.start_run(rid)
    seen = {}
    for _ in range(30):
        sf.advance_run(rid)
        if sf.get_run(rid)["status"] in ("completed", "failed"): break
        claim = sf.claim_next_step(rid)
        if claim is None: continue
        current = Path(claim.inputs["_output_dir"])
        flags = {}
        if claim.step_id == "plan":
            (current / "items.json").write_text('{"items":["alpha","beta"]}')
        elif claim.step_id == "maker":
            item = claim.inputs["_artifact_candidate"]["item"]
            seen[item] = seen.get(item, 0) + 1
            if seen[item] == 1:
                assert files(current) == {}, (item, files(current))
                (current / "own.txt").write_text(item)
                (current / "keep.txt").write_text(item + "-kept")
            else:
                assert files(current) == {"own.txt": item.encode(), "keep.txt": (item + "-kept").encode()}
                (current / "own.txt").write_text(item + "-revised")
        elif claim.step_id == "review":
            item = sf._conn.execute("SELECT loop_item FROM skillflow_steps WHERE id=?",
                                    (claim.token.step_instance_id,)).fetchone()["loop_item"]
            flags = {"passed": seen[item] > 1}
        sf.confirm_step(claim.token, StepResult(flags=flags))
    assert sf.get_run(rid)["status"] == "completed", sf.get_run(rid)
    assert seen == {"alpha": 2, "beta": 2}
    published = sf._workspace.get_step_dir("p", "loop_revision", "maker")
    for item in seen:
        assert files(published / item) == {"own.txt": (item + "-revised").encode(), "keep.txt": (item + "-kept").encode()}


def test_mixed_outputs_keep_artifact_siblings_and_code_destination(tmp_path):
    from skillflow.graph import StepNode, Transition
    from skillflow.output_targets import git
    repo = tmp_path / "repo"; repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base.txt").write_text("base")
    git(repo, "add", "--", "base.txt"); git(repo, "commit", "-qm", "base")
    sf = SkillFlow(str(tmp_path / "mixed.db"), workspace_base=str(tmp_path / "artifacts"),
        code_path_resolver=lambda pid, run_id=None: repo,
        tool_loader=ToolLoader(Path(skillflow.__file__).parent / "tools"))
    node = StepNode(id="cards", output_mode="content", output_target="artifact",
        output_carry_forward=True, output_fixed={
            "report": {"file": "report.json", "target": "artifact"},
            "notes": {"file": "notes.md", "target": "artifact"},
            "readme": {"file": "README.md", "target": "code"}},
        context=[{"from": "repository", "mode": "tool"}], checkpoint=True,
        transitions=[Transition(to="done")])
    sf.register_graph(PipelineGraph(name="revision", begin="cards", steps=[node, StepNode(id="done")]))
    rid=sf.create_run("revision",project_id="p");sf.start_run(rid);sf.advance_run(rid)
    claim=sf.claim_next_step(rid)
    for name, content in (("report", "{}"), ("notes", "keep"), ("readme", "initial")):
        assert "error" not in call(sf,rid,claim,"write_"+name,content=content)
    claim=revise(sf,rid,claim)
    assert "error" not in call(sf,rid,claim,"write_report",content='{"fixed":true}')
    assert "error" not in call(sf,rid,claim,"write_readme",content="revised")
    sf.confirm_step(claim.token,StepResult())
    artifacts=Path(claim.inputs["_artifact_dir"])
    assert (artifacts/"notes.md").read_text()=="keep"
    assert json.loads((artifacts/"report.json").read_text())=={"fixed":True}
    assert (repo/"README.md").read_text()=="revised"
    assert not (artifacts/"README.md").exists() and not (repo/"report.json").exists()


def test_first_pass_carry_forward_reads_repo_without_explicit_source(tmp_path):
    sf, rid, claim = engine(tmp_path)
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir()
    (repo / "scripts" / "cultivation.gd").write_text("monthly practice")
    assert claim.inputs["_artifact_revision"] is False

    read = call(sf, rid, claim, "read", path="scripts/cultivation.gd")
    assert read["source"] == "repo"
    search = call(sf, rid, claim, "search", pattern="monthly",
                  path="scripts/cultivation.gd")
    assert search["matches"][0]["source"] == "repo"

    sf.release_claim(claim.token, "first-pass retry")
    claim = sf.claim_next_step(rid)
    assert claim.inputs["_artifact_revision"] is False
    assert call(sf, rid, claim, "read",
                path="scripts/cultivation.gd")["source"] == "repo"


def test_reopened_carry_forward_keeps_candidate_only_default(tmp_path):
    sf, rid, claim = engine(tmp_path)
    repo = tmp_path / "repo"
    (repo / "deleted.txt").write_text("unrelated repo file")
    write_set(sf, rid, claim)
    claim = revise(sf, rid, claim)
    assert claim.inputs["_artifact_revision"] is True

    assert "error" in call(sf, rid, claim, "read", path="deleted.txt")


def test_legacy_read_tools_first_pass_candidate_overlays_repo_and_falls_back(
        tmp_path):
    from skillflow.tools.read_file.impl import read_file
    from skillflow.tools.list_tree.impl import list_tree
    candidate = tmp_path / "candidate"
    repo = tmp_path / "repo"
    candidate.mkdir()
    repo.mkdir()
    (candidate / "shared.txt").write_text("candidate")
    (repo / "shared.txt").write_text("repo")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "cultivation.gd").write_text("monthly practice")
    kw = dict(workspace_root=str(repo), step_tmp_dir=str(candidate),
              artifact_candidate=True, artifact_revision=False)

    assert "candidate" in read_file("shared.txt", **kw)["content"]
    assert read_file("scripts/cultivation.gd", **kw)["found_in"] == "project"
    assert list_tree("scripts", **kw)["found_in"] == "project"


def test_legacy_read_tools_use_candidate_instead_of_old_output(tmp_path):
    from skillflow.tools.read_file.impl import read_file
    from skillflow.tools.list_tree.impl import list_tree
    old=tmp_path/"old";current=tmp_path/"current";repo=tmp_path/"repo"
    for root in (old,current,repo):root.mkdir()
    (old/"deleted.txt").write_text("old")
    (repo/"deleted.txt").write_text("unrelated")
    (current/"kept.txt").write_text("current")
    kw=dict(workspace_root=str(repo),step_tmp_dir=str(current),step_dir=str(old),artifact_revision=True)
    assert "error" in read_file("deleted.txt",**kw)
    assert "current" in read_file("kept.txt",**kw)["content"]
    tree=list_tree(**kw)
    assert "kept.txt" in tree["tree"] and "deleted.txt" not in tree["tree"]


def test_mixed_outputs_retain_the_existing_artifact_default_requirement():
    from skillflow.graph import StepNode
    with pytest.raises(ValueError, match="default target=artifact"):
        StepNode(id="mixed", output_mode="content", output_target="code",
                 output_carry_forward=True,
                 output_fixed={"report": {"file": "report.json", "target": "artifact"}})
