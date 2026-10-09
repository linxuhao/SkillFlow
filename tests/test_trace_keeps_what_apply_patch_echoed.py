"""The run trace keeps the apply_patch echo exactly as the agent received it.

The tool_result trace row kept only `written`, `error`, `applied` and `size`
of a tool result. apply_patch now answers an applied edit with `echo`, the
lines the file reads around it, and the agent reads that; the trace dropped
it, so a synthetic fused-line edit could leave only `applied: true` in
the trace. Each claim here has its opposite pole:
  1. a step driven through a runner, whose stub agent calls apply_patch, leaves
     a tool_result row whose `echo` is byte for byte the one the agent got;
  2. no other tool's result is stored whole: a large read stays a bounded
     preview, and an `echo` key on another tool's result is not kept.
"""
import asyncio
import json
from pathlib import Path

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.output_targets import git
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager

TOOLS = Path(__file__).parents[1] / "src/skillflow/tools"
BODY = "".join(f"line {i}\n" for i in range(1, 7))


def init_repo(root, body=BODY):
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "trace-echo-test")
    git(root, "config", "user.email", "trace-echo@test.invalid")
    (root / "a.gd").write_text(body)
    git(root, "add", "--", "a.gd")
    git(root, "commit", "-qm", "baseline")
    return root


def engine(tmp_path, body=BODY):
    root = init_repo(tmp_path / "repo", body)
    sf = SkillFlow(":memory:")
    sf._tool_loader = ToolLoader(TOOLS)
    sf._workspace = WorkspaceManager(str(tmp_path / "artifacts"),
                                     code_path_resolver=lambda pid, run_id=None: root)
    node = StepNode(id="implement", output_mode="write", output_target="code",
                    config={"extra_tools": ["apply_patch"]},
                    context=[{"from": "repository", "mode": "tool"}],
                    transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name="g", begin=node.id, steps=[node]))
    rid = sf.create_run("g", {"project_id": "p"}, project_id="p")
    sf.start_run(rid)
    return sf, rid, root


class StubAgent:
    """A StepRunner whose 'model' is a fixed list of tool calls. It keeps
    every result exactly as execute_tool handed it back: that is what an
    agent receives."""

    def __init__(self, sf, calls):
        self.sf = sf
        self.calls = calls
        self.received = []

    def call(self, claimed, name, params):
        return self.sf.execute_tool(
            name, params, run_id=claimed.token.run_id, step_id=claimed.step_id,
            step_instance_id=claimed.token.step_instance_id,
            claim_epoch=claimed.token.claim_epoch)

    async def execute(self, claimed):
        for make in self.calls:
            name, params = make(self.received)
            result = self.call(claimed, name, params)
            self.received.append((name, result))
            if name == "apply_patch" and result.get("spans"):
                assert result["applied"] is False
                refs = params["references"]
                assert len(refs) == len(result["spans"])
                params = {**params, "references": [
                    {**ref, "sha": span["sha"]}
                    for ref, span in zip(refs, result["spans"])]}
                self.received.append((name, self.call(claimed, name, params)))
        return StepResult(outputs={}, flags={})


def drive(sf, rid, runner):
    """The host loop from skillflow's own docstring, for one step."""
    assert sf.advance_run(rid) is not None
    claimed = sf.claim_next_step(rid)
    assert claimed is not None
    result = asyncio.run(runner.execute(claimed))
    sf.confirm_step(claimed.token, result)
    return claimed


def result_rows(sf, rid, event):
    return [row for row in sf.get_trace(rid, category="tool_result")
            if row["event"] == event]


def read_call(_received):
    return "read", {"path": "a.gd"}


def cite(received):
    return next(r for n, r in received if n == "read")["citation"]["sha"]


def test_the_trace_row_carries_the_echo_the_agent_received(tmp_path):
    sf, rid, root = engine(tmp_path)
    runner = StubAgent(sf, [
        read_call,
        lambda got: ("apply_patch", {"references": [
            {"file": "a.gd", "sha": cite(got), "from_line": 3, "from_col": 0,
             "to_line": 3, "to_col": 6, "new_text": "line three\nline 3b"},
            {"file": "a.gd", "sha": cite(got), "from_line": 6, "from_col": 0,
             "to_line": 6, "to_col": 0, "new_text": "line 5b\n"}]}),
    ])
    drive(sf, rid, runner)
    (name, received), = [(n, r) for n, r in runner.received if n == "apply_patch" and r.get("applied")]
    assert received["applied"] is True, received
    assert (root / "a.gd").read_text() == (
        "line 1\nline 2\nline three\nline 3b\nline 4\nline 5\nline 5b\nline 6\n")
    # The row numbered from_line, wherever the echo's context puts it.
    assert [next(r for r in e["text"].split("\n") if r.startswith(f"{e['from_line']}\t"))
            for e in received["echo"]] == ["3\tline three", "7\tline 5b"]

    rows = result_rows(sf, rid, "apply_patch")
    assert len(rows) == 2 and rows[0]["payload"]["applied"] is False
    row = rows[1]
    kept = row["payload"]
    assert kept["echo"] == received["echo"]
    assert (json.dumps(kept["echo"], ensure_ascii=False).encode("utf-8")
            == json.dumps(received["echo"], ensure_ascii=False).encode("utf-8"))
    for stored, shown in zip(kept["echo"], received["echo"]):
        assert stored["text"].encode("utf-8") == shown["text"].encode("utf-8")
    # The fields the row always kept are still there.
    assert kept["written"] == ["a.gd"] and kept["applied"] is True
    assert set(kept) == {"source", "written", "applied", "echo"}


def test_a_refused_apply_patch_row_has_no_echo(tmp_path):
    sf, rid, _ = engine(tmp_path)
    runner = StubAgent(sf, [
        read_call,
        lambda got: ("apply_patch", {"references": [
            {"file": "a.gd", "sha": "0" * 40, "from_line": 3, "from_col": 0,
             "to_line": 3, "to_col": 6, "new_text": "x"}]}),
    ])
    drive(sf, rid, runner)
    (row,) = result_rows(sf, rid, "apply_patch")
    assert row["payload"]["applied"] is False and "echo" not in row["payload"]


def test_another_tools_large_result_is_still_not_stored(tmp_path):
    big = "".join(f"filler line {i:05d} " + "x" * 60 + "\n" for i in range(3000))
    sf, rid, _ = engine(tmp_path, big)
    runner = StubAgent(sf, [read_call])
    drive(sf, rid, runner)
    (_, received), = runner.received
    served = json.dumps(received, ensure_ascii=False)
    assert len(served) > 20000
    (row,) = result_rows(sf, rid, "read")
    stored = json.dumps(row["payload"], ensure_ascii=False)
    assert set(row["payload"]) == {"source", "preview"}
    assert len(stored) < 2200
    assert "filler line 00100" not in stored


def test_an_echo_on_another_tools_result_is_not_kept(tmp_path, monkeypatch):
    """The echo is kept because apply_patch sent it, not because a result
    has a key of that name."""
    sf, rid, _ = engine(tmp_path)
    loud = {"written": ["a.gd"], "echo": [{"file": "a.gd", "text": "y" * 50000}]}
    monkeypatch.setattr(sf, "_execute_tool_impl",
                        lambda name, params, **kw: dict(loud))
    runner = StubAgent(sf, [lambda got: ("edit", {"file": "a.gd",
                                                  "old_str": "a", "new_str": "b"})])
    drive(sf, rid, runner)
    (row,) = result_rows(sf, rid, "edit")
    assert row["payload"] == {"source": "agent", "written": ["a.gd"]}
