"""Per-item loop I/O: a loop-body step's output is keyed by loop item so each
iteration survives, and a downstream reader routes by `scope` (task=own item,
all=every item). Regression guard for the fan-out aggregation bug where the
shared {step}/ dir was replaced each iteration → aggregator saw only the last."""

from pathlib import Path

from skillflow.workspace import WorkspaceManager, _sanitize_item
from skillflow.read_tools import resolve_context_paths


def test_sanitize_item_is_filesystem_safe():
    assert _sanitize_item("claim_2") == "claim_2"
    assert _sanitize_item("../etc/passwd") == "etc_passwd"      # no traversal
    assert _sanitize_item("Task A/B:1") == "Task_A_B_1"
    assert _sanitize_item("") == "item"


def test_get_step_dir_item_keying(tmp_path):
    ws = WorkspaceManager(str(tmp_path), projects_base=str(tmp_path / "proj"))
    plain = ws.get_step_dir("p", "cfg", "verify")
    keyed = ws.get_step_dir("p", "cfg", "verify", item="claim_1")
    assert plain.name == "verify"
    assert keyed == plain / "claim_1"
    # tmp mirrors it
    assert ws.get_step_tmp_dir("p", "cfg", "verify", item="claim_1").parent.name == "verify.tmp"


def _seed_two_items(tmp_path):
    """A loop-body producer 'verify' with per-item folders for two items."""
    base = tmp_path / "cfg" / "verify"
    (base / "claim_0").mkdir(parents=True)
    (base / "claim_1").mkdir(parents=True)
    (base / "claim_0" / "v.json").write_text('{"claim":0}')
    (base / "claim_1" / "v.json").write_text('{"claim":1}')
    return str(tmp_path)


def test_scope_task_reads_own_item_folder(tmp_path):
    ws_root = _seed_two_items(tmp_path)
    lc = {"_loop_body_steps": {"verify"}, "_current_item": "claim_1"}
    # default (scope omitted) == task → own item's folder
    roots = resolve_context_paths({"source_type": "step", "step_id": "verify"},
                                  ws_root, current_config="cfg", loop_context=lc)
    assert roots == [str(tmp_path / "cfg" / "verify" / "claim_1")]


def test_scope_all_reads_the_parent_so_rglob_sees_every_item(tmp_path):
    ws_root = _seed_two_items(tmp_path)
    lc = {"_loop_body_steps": {"verify"}, "_current_item": "claim_1"}
    roots = resolve_context_paths(
        {"source_type": "step", "step_id": "verify", "scope": "all"},
        ws_root, current_config="cfg", loop_context=lc)
    # parent dir → the read surface rglobs into every {item}/ subdir
    assert roots == [str(tmp_path / "cfg" / "verify")]
    found = sorted(p.name for p in Path(roots[0]).rglob("v.json"))
    assert found == ["v.json", "v.json"]  # both items present


def test_non_loop_producer_is_unaffected(tmp_path):
    # A step NOT in _loop_body_steps keeps the plain {step}/ path (back-compat).
    (tmp_path / "cfg" / "plan").mkdir(parents=True)
    (tmp_path / "cfg" / "plan" / "x.md").write_text("hi")
    lc = {"_loop_body_steps": {"verify"}, "_current_item": "claim_1"}
    roots = resolve_context_paths({"source_type": "step", "step_id": "plan"},
                                  str(tmp_path), current_config="cfg", loop_context=lc)
    assert roots == [str(tmp_path / "cfg" / "plan")]


# ── Full-run: per-item promotion preserves siblings (the actual bug fix) ──

def test_fanout_loop_preserves_every_item_folder(sf_with_workspace):
    """Drive a real fan-out loop; every iteration's output must survive under
    {verify}/{item}/ instead of the shared {verify}/ being replaced each round."""
    from skillflow.core import StepResult
    from skillflow.graph import PipelineGraph

    sf = sf_with_workspace
    for role in ("planner", "verifier", "aggregator"):
        sf.register_agent_config(role, model="mock", tools=[])
    graph = PipelineGraph._from_dict({
        "name": "fan", "description": "x", "begin": "make_manifest",
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": "done", "result": "completed"}]},
        "steps": [
            {"id": "make_manifest", "step_type": "agent", "agent_config": "planner",
             "output": {"mode": "write"},
             "transitions": [{"to": "loop"}]},
            {"id": "loop", "step_type": "loop",
             "loop": {"source": {"step": "make_manifest", "file": "items.json",
                                 "field": "execution_order"}, "item_as": "item",
                      "max_iterations": 5},
             "transitions": [{"to": "verify", "max_loop": 5}, {"to": "aggregate"}]},
            {"id": "verify", "step_type": "agent", "agent_config": "verifier",
             "output": {"mode": "write"},
             "transitions": [{"to": "loop", "max_loop": 5}]},
            {"id": "aggregate", "step_type": "agent", "agent_config": "aggregator",
             "output": {"mode": "write"},
             "transitions": [{"to": "done"}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    })
    sf.register_graph(graph)
    rid = sf.get_or_create_run("fan", "proj1", {"project_id": "proj1"})
    sf.start_run(rid)
    pid, gname = "proj1", "fan"

    # step_id → files to stage before confirming (manifest for planner, a
    # per-item verdict for each verify iteration)
    files_for = {
        "make_manifest": {"items.json": '{"execution_order": [["alpha", "beta"]]}'},
        "verify": {"v.json": "verdict"},
    }
    for _ in range(30):
        run = sf.get_run(rid)
        if run["status"] in ("completed", "failed"):
            break
        sf.advance_run(rid)
        if sf.get_run(rid)["status"] == "paused":
            sf.resume_run(rid)
            continue
        claimed = sf.claim_next_step(rid)
        if claimed is None:
            continue
        _tmp = sf._workspace.get_step_tmp_dir(pid, gname, claimed.step_id)
        for fn, c in (files_for.get(claimed.step_id) or {}).items():
            (_tmp / fn).write_text(c, encoding="utf-8")
        sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))

    verify_dir = sf._workspace.get_step_dir(pid, gname, "verify")
    items = sorted(p.name for p in verify_dir.iterdir() if p.is_dir())
    assert items == ["alpha", "beta"], f"sibling items lost: {items}"
    # both verdict files survive (the bug lost all but the last)
    assert (verify_dir / "alpha" / "v.json").exists()
    assert (verify_dir / "beta" / "v.json").exists()
