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


# ── Gap-fill regression tests (one per confirmed review finding) ──────────

def test_context_inline_named_file_aggregates_all_items(tmp_path):
    """Finding 4, context.py half: an outside reader's inline source naming a
    file must concatenate every item's copy, not flat-miss to empty context."""
    from skillflow.context import ContextResolver
    _seed_two_items(tmp_path)
    cr = ContextResolver(Path(tmp_path))
    lc = _lc(reader_loop="", items={"loopA": "claim_1"})   # outside reader
    label, content = cr._resolve_step_output(
        {"step": "verify", "file": "v.json"}, "cfg", loop_context=lc)
    assert '{"claim":0}' in content and '{"claim":1}' in content
    assert "claim_0" in content and "claim_1" in content   # per-item headers


def _drive_reject_once(sf, graph, files_for, reject_file):
    """Drive a fanout graph whose reviewer rejects the first round (so the maker
    re-claims the SAME item with a promoted prior output). Returns run id and a
    flag-flipper the caller controls via files written per claim."""
    sf.register_graph(graph)
    rid = sf.get_or_create_run("g", "projr", {"project_id": "projr"})
    sf.start_run(rid)
    return rid


def test_edit_fallback_and_self_read_see_prior_item_output(sf_with_workspace):
    """Finding 5: a loop-body maker re-running after a review reject must find
    its own PRIOR promoted output at {step}/{item}/ — edit baseline + step_dir."""
    from skillflow.core import StepResult
    sf = sf_with_workspace
    for role in ("planner", "maker", "reviewer"):
        sf.register_agent_config(role, model="mock", tools=[])
    graph = _graph([
        {"id": "plan", "step_type": "agent", "agent_config": "planner",
         "output": {"mode": "write"}, "transitions": [{"to": "loop"}]},
        {"id": "loop", "step_type": "loop",
         "loop": {"source": {"step": "plan", "file": "items.json",
                             "field": "execution_order"}, "item_as": "item",
                  "max_iterations": 5},
         "transitions": [{"to": "impl", "max_loop": 5}, {"to": "done"}]},
        {"id": "impl", "step_type": "agent", "agent_config": "maker",
         "output": {"mode": "write"}, "transitions": [{"to": "review"}]},
        {"id": "review", "step_type": "agent", "agent_config": "reviewer",
         "output": {"mode": "write"},
         "transitions": [
             {"to": "loop", "match": {"from_file": "verdict.json", "field": "passed",
                                      "value": True}, "max_loop": 5},
             {"to": "impl", "match": {"from_file": "verdict.json", "field": "passed",
                                      "value": False}, "max_loop": 3}]},
        {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
    ])
    sf.register_graph(graph)
    rid = sf.get_or_create_run("g", "projr", {"project_id": "projr"})
    sf.start_run(rid)
    pid, gname = "projr", "g"
    verdicts = iter([False, True, True])   # round 1 rejected → impl re-runs

    for _ in range(40):
        if sf.get_run(rid)["status"] in ("completed", "failed"):
            break
        sf.advance_run(rid)
        if sf.get_run(rid)["status"] == "paused":
            sf.resume_run(rid)
            continue
        claimed = sf.claim_next_step(rid)
        if claimed is None:
            continue
        _tmp = sf._workspace.get_step_tmp_dir(pid, gname, claimed.step_id)
        if claimed.step_id == "plan":
            (_tmp / "items.json").write_text('{"execution_order": [["alpha"]]}')
        elif claimed.step_id == "impl":
            # SECOND claim of impl (the reject re-run): its prior promoted output
            # must be visible as edit baseline + step_dir fallback, per-item.
            prior = sf._workspace.get_step_dir(pid, gname, "impl", item="alpha")
            if prior.is_dir():
                fb = sf._edit_fallback_dir(rid, pid, gname, "impl")
                assert fb.endswith("impl/alpha"), f"edit fallback not item-aware: {fb}"
                assert (Path(fb) / "draft.md").exists()
            (_tmp / "draft.md").write_text("v2")
        elif claimed.step_id == "review":
            import json as _json
            (_tmp / "verdict.json").write_text(
                _json.dumps({"passed": next(verdicts)}))
        sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))

    assert sf.get_run(rid)["status"] == "completed"
    # the re-run replaced only alpha's folder; final content is round 2's
    assert (sf._workspace.get_step_dir(pid, gname, "impl", item="alpha")
            / "draft.md").read_text() == "v2"


def test_two_loops_get_their_own_items(sf_with_workspace):
    """Finding 6: per-loop item lookups — while loop B runs, its body steps must
    key by B's item, never leak drained loop A's last item."""
    from skillflow.core import StepResult
    sf = sf_with_workspace
    for role in ("planner", "wa", "wb"):
        sf.register_agent_config(role, model="mock", tools=[])
    graph = _graph([
        {"id": "plan", "step_type": "agent", "agent_config": "planner",
         "output": {"mode": "write"}, "transitions": [{"to": "loopA"}]},
        {"id": "loopA", "step_type": "loop",
         "loop": {"source": {"step": "plan", "file": "a.json",
                             "field": "execution_order"}, "item_as": "ita",
                  "max_iterations": 5},
         "transitions": [{"to": "bodyA", "max_loop": 5}, {"to": "loopB"}]},
        {"id": "bodyA", "step_type": "agent", "agent_config": "wa",
         "output": {"mode": "write"}, "transitions": [{"to": "loopA", "max_loop": 5}]},
        {"id": "loopB", "step_type": "loop",
         "loop": {"source": {"step": "plan", "file": "b.json",
                             "field": "execution_order"}, "item_as": "itb",
                  "max_iterations": 5},
         "transitions": [{"to": "bodyB", "max_loop": 5}, {"to": "done"}]},
        {"id": "bodyB", "step_type": "agent", "agent_config": "wb",
         "output": {"mode": "write"}, "transitions": [{"to": "loopB", "max_loop": 5}]},
        {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
    ])
    sf.register_graph(graph)
    rid = sf.get_or_create_run("g", "proj2l", {"project_id": "proj2l"})
    sf.start_run(rid)
    pid, gname = "proj2l", "g"
    resolver = sf._get_resolver("g")
    checked_b = False

    for _ in range(40):
        if sf.get_run(rid)["status"] in ("completed", "failed"):
            break
        sf.advance_run(rid)
        if sf.get_run(rid)["status"] == "paused":
            sf.resume_run(rid)
            continue
        claimed = sf.claim_next_step(rid)
        if claimed is None:
            continue
        _tmp = sf._workspace.get_step_tmp_dir(pid, gname, claimed.step_id)
        if claimed.step_id == "plan":
            (_tmp / "a.json").write_text('{"execution_order": [["a1"]]}')
            (_tmp / "b.json").write_text('{"execution_order": [["b1"]]}')
        else:
            (_tmp / "out.md").write_text(claimed.step_id)
        if claimed.step_id == "bodyB":
            # loop A is drained (its state row persists with current_item=a1) —
            # bodyB must key by loop B's item, bodyA's lookup stays A's.
            assert sf._loop_item_for_step(rid, resolver, "bodyB") == "b1"
            assert sf._loop_item_for_step(rid, resolver, "bodyA") == "a1"
            # legacy $var injection carries the READER's loop key only
            rc = claimed.inputs.get("_resolved_context", {})
            assert rc.get("[itb]") == "b1"
            assert "[ita]" not in rc
            checked_b = True
        sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))

    assert checked_b and sf.get_run(rid)["status"] == "completed"
    assert (sf._workspace.get_step_dir(pid, gname, "bodyA", item="a1") / "out.md").exists()
    assert (sf._workspace.get_step_dir(pid, gname, "bodyB", item="b1") / "out.md").exists()


def test_cjk_items_promote_distinct_and_paths_report_sanitized(sf_with_workspace):
    """Findings 2+7 e2e: CJK items get DISTINCT folders (no rmtree collision) and
    reported paths (moved_files, write feedback) use the on-disk sanitized name."""
    from skillflow.core import StepResult
    sf = sf_with_workspace
    for role in ("planner", "verifier"):
        sf.register_agent_config(role, model="mock", tools=[])
    graph = _graph([
        {"id": "plan", "step_type": "agent", "agent_config": "planner",
         "output": {"mode": "write"}, "transitions": [{"to": "loop"}]},
        {"id": "loop", "step_type": "loop",
         "loop": {"source": {"step": "plan", "file": "items.json",
                             "field": "execution_order"}, "item_as": "item",
                  "max_iterations": 5},
         "transitions": [{"to": "verify", "max_loop": 5}, {"to": "done"}]},
        {"id": "verify", "step_type": "agent", "agent_config": "verifier",
         "output": {"mode": "write"}, "transitions": [{"to": "loop", "max_loop": 5}]},
        {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
    ])
    sf.register_graph(graph)
    rid = sf.get_or_create_run("g", "projc", {"project_id": "projc"})
    sf.start_run(rid)
    pid, gname = "projc", "g"
    import json as _json
    enriched_paths = []

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
        _tmp = sf._workspace.get_step_tmp_dir(pid, gname, claimed.step_id)
        if claimed.step_id == "plan":
            (_tmp / "items.json").write_text(_json.dumps(
                {"execution_order": [["中文任务", "另一个"]]}, ensure_ascii=False))
        else:
            (_tmp / "v.json").write_text("x")
            # Finding 7: the write-feedback path must advertise the sanitized
            # folder (the one that will exist), not the raw CJK item.
            r = sf._enrich_write_path(rid, "verify", {"written": "v.json"})
            enriched_paths.append(r["path"])
        sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))

    assert sf.get_run(rid)["status"] == "completed"
    verify_dir = sf._workspace.get_step_dir(pid, gname, "verify")
    folders = sorted(p.name for p in verify_dir.iterdir() if p.is_dir())
    assert len(folders) == 2, f"CJK items collided: {folders}"   # both survive
    for p in enriched_paths:
        # advertised path exists relative to the config dir after promotion
        assert (sf._workspace.get_config_path(pid, gname) / p).exists() or \
               any(p.startswith(f"verify/{f}") for f in folders)
    for f in folders:
        assert (verify_dir / f / "v.json").exists()


def test_flat_pre_upgrade_leftovers_cleaned_at_promotion(sf_with_workspace):
    """Finding 8a: stale FLAT files under {step}/ (pre-1.5.23 layout) are removed
    when a per-item promotion lands, so all-items readers can't double-read."""
    from skillflow.core import StepResult
    sf = sf_with_workspace
    for role in ("planner", "verifier"):
        sf.register_agent_config(role, model="mock", tools=[])
    graph = _graph([
        {"id": "plan", "step_type": "agent", "agent_config": "planner",
         "output": {"mode": "write"}, "transitions": [{"to": "loop"}]},
        {"id": "loop", "step_type": "loop",
         "loop": {"source": {"step": "plan", "file": "items.json",
                             "field": "execution_order"}, "item_as": "item",
                  "max_iterations": 5},
         "transitions": [{"to": "verify", "max_loop": 5}, {"to": "done"}]},
        {"id": "verify", "step_type": "agent", "agent_config": "verifier",
         "output": {"mode": "write"}, "transitions": [{"to": "loop", "max_loop": 5}]},
        {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
    ])
    sf.register_graph(graph)
    rid = sf.get_or_create_run("g", "projf", {"project_id": "projf"})
    sf.start_run(rid)
    pid, gname = "projf", "g"
    # simulate a pre-upgrade flat leftover
    stale_dir = sf._workspace.get_step_dir(pid, gname, "verify")
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / "old_flat.json").write_text("stale")

    from skillflow.core import StepResult
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
        _tmp = sf._workspace.get_step_tmp_dir(pid, gname, claimed.step_id)
        if claimed.step_id == "plan":
            (_tmp / "items.json").write_text('{"execution_order": [["only"]]}')
        else:
            (_tmp / "v.json").write_text("x")
        sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))

    assert not (stale_dir / "old_flat.json").exists(), "flat leftover not GC'd"
    assert (stale_dir / "only" / "v.json").exists()


def test_dropped_manifest_items_are_gcd(sf_with_workspace):
    """Finding 8b: per-item folders of items dropped from a regenerated manifest
    are removed (direct unit of _gc_dropped_item_dirs)."""
    sf = sf_with_workspace
    for role in ("planner", "verifier"):
        sf.register_agent_config(role, model="mock", tools=[])
    graph = _graph([
        {"id": "plan", "step_type": "agent", "agent_config": "planner",
         "output": {"mode": "write"}, "transitions": [{"to": "loop"}]},
        {"id": "loop", "step_type": "loop",
         "loop": {"source": {"step": "plan", "file": "items.json",
                             "field": "execution_order"}, "item_as": "item",
                  "max_iterations": 5},
         "transitions": [{"to": "verify", "max_loop": 5}, {"to": "done"}]},
        {"id": "verify", "step_type": "agent", "agent_config": "verifier",
         "output": {"mode": "write"}, "transitions": [{"to": "loop", "max_loop": 5}]},
        {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
    ])
    sf.register_graph(graph)
    pid, gname = "projg", "g"
    d = sf._workspace.get_step_dir(pid, gname, "verify")
    for it in ("keep_me", "drop_me"):
        (d / it).mkdir(parents=True, exist_ok=True)
        (d / it / "v.json").write_text("x")
    sf._gc_dropped_item_dirs(pid, gname, "loop", ["keep_me", "new_one"])
    assert (d / "keep_me" / "v.json").exists()
    assert not (d / "drop_me").exists()
