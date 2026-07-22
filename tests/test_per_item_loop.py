"""Per-item loop I/O: a loop-body AGENT step's output is keyed by loop item so
each iteration survives, and readers route by position + `scope` (same-loop
reader = own item; outside reader or scope:all = every item). Regression guard
for the fan-out aggregation bug where the shared {step}/ dir was replaced each
iteration → aggregator saw only the last item."""

from pathlib import Path

import pytest

from skillflow.graph import PipelineGraph, loop_body_map
from skillflow.workspace import (WorkspaceManager, _sanitize_item,
                                 route_step_read_dir)
from skillflow.read_tools import resolve_context_paths


# ── Sanitizer: filesystem-safe AND collision-free ─────────────────────────

def test_sanitize_item_is_filesystem_safe():
    assert _sanitize_item("claim_2") == "claim_2"          # clean → unchanged
    assert "/" not in _sanitize_item("../etc/passwd")      # no traversal
    assert "/" not in _sanitize_item("Task A/B:1")


def test_sanitize_item_distinct_raw_items_never_collide():
    # The lossy transform appends a hash of the raw value — the collisions that
    # destroyed sibling outputs (rmtree on promote) can no longer happen.
    assert _sanitize_item("api/auth") != _sanitize_item("api_auth")
    assert _sanitize_item("中文任务") != _sanitize_item("另一个")      # CJK ≠ CJK
    assert _sanitize_item("中文任务") != _sanitize_item("item")
    a = "x" * 119 + "A"
    b = "x" * 119 + "B"
    assert _sanitize_item(a * 2) != _sanitize_item(b * 2)  # >cap, differ past cap
    # Deterministic (readers and writers agree)
    assert _sanitize_item("中文任务") == _sanitize_item("中文任务")


def test_get_step_dir_item_keying(tmp_path):
    ws = WorkspaceManager(str(tmp_path), projects_base=str(tmp_path / "proj"))
    plain = ws.get_step_dir("p", "cfg", "verify")
    keyed = ws.get_step_dir("p", "cfg", "verify", item="claim_1")
    assert plain.name == "verify"
    assert keyed == plain / "claim_1"


# ── Routing: position-aware, per-loop ─────────────────────────────────────

def _lc(reader_loop="", items=None):
    return {"_loop_of": {"verify": "loopA"},
            "_reader_loop": reader_loop,
            "_loop_items": items or {}}


def _seed_two_items(tmp_path):
    base = tmp_path / "cfg" / "verify"
    (base / "claim_0").mkdir(parents=True)
    (base / "claim_1").mkdir(parents=True)
    (base / "claim_0" / "v.json").write_text('{"claim":0}')
    (base / "claim_1" / "v.json").write_text('{"claim":1}')
    return str(tmp_path)


def test_same_loop_reader_gets_own_item_folder(tmp_path):
    ws_root = _seed_two_items(tmp_path)
    lc = _lc(reader_loop="loopA", items={"loopA": "claim_1"})
    roots = resolve_context_paths({"source_type": "step", "step_id": "verify"},
                                  ws_root, current_config="cfg", loop_context=lc)
    assert roots == [str(tmp_path / "cfg" / "verify" / "claim_1")]


def test_outside_reader_defaults_to_all_items(tmp_path):
    # An aggregator AFTER the loop (reader not in the producer's loop) must get
    # the parent even though the drained loop's current_item is still set — the
    # stale last item can never route an aggregator to one folder.
    ws_root = _seed_two_items(tmp_path)
    lc = _lc(reader_loop="", items={"loopA": "claim_1"})  # stale item present
    roots = resolve_context_paths({"source_type": "step", "step_id": "verify"},
                                  ws_root, current_config="cfg", loop_context=lc)
    assert roots == [str(tmp_path / "cfg" / "verify")]


def test_other_loop_reader_defaults_to_all_items(tmp_path):
    ws_root = _seed_two_items(tmp_path)
    lc = _lc(reader_loop="loopB", items={"loopA": "claim_1", "loopB": "x"})
    roots = resolve_context_paths({"source_type": "step", "step_id": "verify"},
                                  ws_root, current_config="cfg", loop_context=lc)
    assert roots == [str(tmp_path / "cfg" / "verify")]


def test_scope_all_overrides_same_loop_reader(tmp_path):
    ws_root = _seed_two_items(tmp_path)
    lc = _lc(reader_loop="loopA", items={"loopA": "claim_1"})
    roots = resolve_context_paths(
        {"source_type": "step", "step_id": "verify", "scope": "all"},
        ws_root, current_config="cfg", loop_context=lc)
    assert roots == [str(tmp_path / "cfg" / "verify")]
    found = sorted(p.name for p in Path(roots[0]).rglob("v.json"))
    assert found == ["v.json", "v.json"]


def test_all_items_mode_file_selector_finds_per_item_files(tmp_path):
    # The aggregation-with-file-selector hole: {step: verify, file: v.json,
    # scope: all} must return every item's v.json, not a flat miss.
    ws_root = _seed_two_items(tmp_path)
    lc = _lc(reader_loop="", items={"loopA": "claim_1"})
    roots = resolve_context_paths(
        {"source_type": "step", "step_id": "verify", "files": ["v.json"],
         "scope": "all"},
        ws_root, current_config="cfg", loop_context=lc)
    assert sorted(roots) == [
        str(tmp_path / "cfg" / "verify" / "claim_0" / "v.json"),
        str(tmp_path / "cfg" / "verify" / "claim_1" / "v.json"),
    ]


def test_non_loop_producer_is_unaffected(tmp_path):
    (tmp_path / "cfg" / "plan").mkdir(parents=True)
    (tmp_path / "cfg" / "plan" / "x.md").write_text("hi")
    lc = _lc(reader_loop="loopA", items={"loopA": "claim_1"})
    roots = resolve_context_paths({"source_type": "step", "step_id": "plan"},
                                  str(tmp_path), current_config="cfg",
                                  loop_context=lc)
    assert roots == [str(tmp_path / "cfg" / "plan")]


def test_route_helper_is_the_single_rule(tmp_path):
    base = tmp_path / "s"
    lc = _lc(reader_loop="loopA", items={"loopA": "it1"})
    assert route_step_read_dir(base, "verify", "task", lc) == base / "it1"
    assert route_step_read_dir(base, "verify", "all", lc) == base
    assert route_step_read_dir(base, "other", "task", lc) == base   # non-loop producer
    lc_out = _lc(reader_loop="", items={"loopA": "it1"})
    assert route_step_read_dir(base, "verify", "task", lc_out) == base


# ── Topology: reach-back body definition ──────────────────────────────────

def _graph(steps):
    return PipelineGraph._from_dict({
        "name": "g", "description": "x", "begin": steps[0]["id"],
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": "done", "result": "completed"}]},
        "steps": steps,
    })


def test_loop_body_excludes_giveup_edge_targets():
    # verify --(give-up)--> summarize: summarize never returns to the loop, so
    # it is NOT body — it runs once, post-loop, and must read ALL items.
    g = _graph([
        {"id": "loop", "step_type": "loop",
         "loop": {"source": {"step": "loop", "file": "m.json", "field": "x"},
                  "item_as": "it", "max_iterations": 5},
         "transitions": [{"to": "verify", "max_loop": 5}, {"to": "summarize"}]},
        {"id": "verify", "step_type": "agent", "agent_config": "v",
         "transitions": [
             {"to": "loop", "match": {"from_file": "v.json", "field": "p", "value": True},
              "max_loop": 5},
             {"to": "summarize", "match": {"from_file": "v.json", "field": "p", "value": False},
              "max_loop": 3}]},
        {"id": "summarize", "step_type": "agent", "agent_config": "s",
         "transitions": [{"to": "done"}]},
        {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
    ])
    bodies = loop_body_map(g.steps)
    assert bodies["loop"] == frozenset({"verify"})


# ── Full-run: per-item promotion preserves siblings (the actual bug fix) ──

def test_fanout_loop_preserves_every_item_folder(sf_with_workspace):
    from skillflow.core import StepResult

    sf = sf_with_workspace
    for role in ("planner", "verifier", "aggregator"):
        sf.register_agent_config(role, model="mock", tools=[])
    graph = _graph([
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
    ])
    sf.register_graph(graph)
    rid = sf.get_or_create_run("g", "proj1", {"project_id": "proj1"})
    sf.start_run(rid)
    pid, gname = "proj1", "g"

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
    assert (verify_dir / "alpha" / "v.json").exists()
    assert (verify_dir / "beta" / "v.json").exists()


# ── $STEP_DIR lifecycle hooks are item-aware (the repo_apply regression) ──

def test_resolve_variables_step_dir_honors_item(tmp_path):
    ws = WorkspaceManager(str(tmp_path), projects_base=str(tmp_path / "proj"))
    flat = ws.resolve_variables("p", "cfg", "impl", {"src": "$STEP_DIR"})
    keyed = ws.resolve_variables("p", "cfg", "impl", {"src": "$STEP_DIR"},
                                 item="task_1")
    assert flat["src"].endswith("impl")
    assert keyed["src"].endswith("impl/task_1")


def test_scope_typo_fails_at_registration():
    with pytest.raises(Exception):
        _graph([
            {"id": "a", "step_type": "agent", "agent_config": "x",
             "context": [{"source": {"step": "a", "scope": "al"}}],
             "transitions": [{"to": "done"}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ])


def test_on_deliver_hook_gets_per_item_step_dir(sf_with_workspace):
    """$STEP_DIR in an on_deliver lifecycle hook must resolve to the per-item
    promotion target {step}/{item}/ — the flat parent would hand repo_apply a
    tree of item-named folders to commit (the DPE repo-corruption regression)."""
    from skillflow.core import StepResult

    sf = sf_with_workspace
    seen: list[str] = []
    sf._tool_loader.register(
        "probe_deliver", lambda source_dir="", **kw: seen.append(source_dir) or
        {"applied": True})
    for role in ("planner", "worker"):
        sf.register_agent_config(role, model="mock", tools=[])
    graph = _graph([
        {"id": "plan", "step_type": "agent", "agent_config": "planner",
         "output": {"mode": "write"},
         "transitions": [{"to": "loop"}]},
        {"id": "loop", "step_type": "loop",
         "loop": {"source": {"step": "plan", "file": "items.json",
                             "field": "execution_order"}, "item_as": "task",
                  "max_iterations": 5},
         "transitions": [{"to": "impl", "max_loop": 5}, {"to": "done"}]},
        {"id": "impl", "step_type": "agent", "agent_config": "worker",
         "output": {"mode": "write"},
         "lifecycle": {"on_deliver": {"tool": "probe_deliver",
                                      "params": {"source_dir": "$STEP_DIR"}}},
         "transitions": [{"to": "loop", "max_loop": 5}]},
        {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
    ])
    sf.register_graph(graph)
    rid = sf.get_or_create_run("g", "projh", {"project_id": "projh"})
    sf.start_run(rid)
    files_for = {
        "plan": {"items.json": '{"execution_order": [["t1", "t2"]]}'},
        "impl": {"src.py": "print(1)"},
    }
    for _ in range(30):
        if sf.get_run(rid)["status"] in ("completed", "failed"):
            break
        sf.advance_run(rid)
        if sf.get_run(rid)["status"] == "paused":
            sf.resume_run(rid)
            continue
        claimed = sf.claim_next_step(rid)
        if claimed is None:
            continue
        _tmp = sf._workspace.get_step_tmp_dir("projh", "g", claimed.step_id)
        for fn, c in (files_for.get(claimed.step_id) or {}).items():
            (_tmp / fn).write_text(c, encoding="utf-8")
        sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))

    assert len(seen) == 2, f"hook should fire once per item, got {seen}"
    assert seen[0].endswith("impl/t1"), seen
    assert seen[1].endswith("impl/t2"), seen
    # and the delivered dir contains the item's files directly (no item nesting)
    assert (Path(seen[0]) / "src.py").exists()


def test_fanout_scope_fixture_registers():
    """The documented fan-out syntax (fixture) parses, validates, and carries
    the normalized scope field through a to_dict round-trip."""
    g = PipelineGraph.from_yaml("tests/fixtures/fanout_scope.yaml")
    agg = next(s for s in g.steps if s.id == "aggregate")
    assert agg.context[0]["scope"] == "all"
    rev = next(s for s in g.steps if s.id == "review")
    assert rev.context[0]["scope"] == "task"          # default, normalized in
    g2 = PipelineGraph._from_dict(g.to_dict())        # round-trip keeps scope
    agg2 = next(s for s in g2.steps if s.id == "aggregate")
    assert agg2.context[0]["scope"] == "all"
    assert loop_body_map(g.steps)["fan_loop"] == frozenset({"extract", "review"})
