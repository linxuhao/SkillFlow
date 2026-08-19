"""A step that produced nothing must not run its deliver hooks.

Live, NL2Repo task ``funcy`` (loop items ``fix_tree_map_double_apply`` and
``fix_tree_map_node_once``): the ``t_impl`` agent answered in 92 completion tokens
and wrote no file. ``_step_commit`` promoted nothing — it does not even create
``{step}/{item}/`` for an empty staging dir — so the ``on_deliver`` hook's
``repo_apply`` failed with ``Source dir not found``, retried itself three times (a
TOOL retried for output only the AGENT could write), and the step then hard-failed
into the graph's ``_error`` edge, which forwarded it to ``t_impl_review``. The
reviewer reviewed an item that does not exist, and the durable trace showed only
"hook failed … next step claimed": no ``completed``, no failure, no reason.

The engine now checks BEFORE the deliver hooks: nothing promoted → nothing to
deliver → skip the hooks and re-ask the step on its own (shared) retry budget,
with the reason in its prompt. Unlike validation exhaustion (1.5.31), which keeps
the work it could not certify, there is no work here to keep and no reviewer that
could judge it — the only useful response is another attempt.
"""

from pathlib import Path

from mocks import MockToolLoader
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.workspace import WorkspaceManager


class _Delivery:
    """Stand-in for `repo_apply`, recording every call it receives."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, source_dir: str, **kwargs) -> dict:
        self.calls.append(source_dir)
        src = Path(source_dir)
        if not src.exists():
            return {"applied": False, "files": [],
                    "error": f"Source dir not found: {source_dir}"}
        return {"applied": True, "files": [str(p.name) for p in src.rglob("*")],
                "committed": True}


def _graph(name: str, *, error_edge: bool = True,
           max_retries: int = 1) -> PipelineGraph:
    transitions = [Transition(to="review")]
    if error_edge:
        # dpe_default's own escape: without it, validation exhaustion fails the
        # whole RUN and discards every task the loop already finished.
        transitions.append(Transition(to="review", match={"_error": True}))
    return PipelineGraph(name=name, begin="impl", steps=[
        StepNode(id="impl", step_type="agent", output_mode="write",
                 max_retries=max_retries,
                 lifecycle={"on_deliver": [{"tool": "repo_apply",
                                            "params": {"source_dir": "$STEP_DIR"},
                                            "on_failure": "retry",
                                            "max_retries": 3}]},
                 transitions=transitions),
        StepNode(id="review", step_type="agent", transitions=[Transition(to=None)]),
    ])


def _sf(tmp_path: Path, graph: PipelineGraph) -> tuple[SkillFlow, _Delivery]:
    delivery = _Delivery()
    sf = SkillFlow(":memory:")
    sf.register_graph(graph)
    sf._tool_loader = MockToolLoader({"repo_apply": delivery})
    sf._workspace = WorkspaceManager(str(tmp_path / "ws"))
    return sf, delivery


def _start(sf: SkillFlow, name: str) -> str:
    rid = sf.create_run(name, {"project_id": "pid"})
    sf.start_run(rid)
    return rid


def _confirm(sf: SkillFlow, rid: str, files: dict[str, str] | None = None):
    """Claim `impl`, stage `files` (nothing by default), confirm."""
    claimed = sf.claim_next_step(rid)
    assert claimed is not None and claimed.step_id == "impl"
    tmp = sf._workspace.get_step_tmp_dir("pid", sf._get_graph_name(rid), "impl")
    for rel, body in (files or {}).items():
        (tmp / rel).write_text(body)
    sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))
    return claimed


def _step_row(sf: SkillFlow, rid: str, step_id: str = "impl"):
    return sf._conn.execute(
        "SELECT status, last_error FROM skillflow_steps "
        "WHERE run_id = ? AND step_id = ? ORDER BY id DESC LIMIT 1",
        (rid, step_id),
    ).fetchone()


def _node(sf: SkillFlow, rid: str) -> str:
    return sf._conn.execute(
        "SELECT current_node FROM skillflow_runs WHERE id = ?", (rid,)
    ).fetchone()["current_node"]


def test_an_empty_step_never_reaches_the_deliver_hook(tmp_path):
    sf, delivery = _sf(tmp_path, _graph("empty"))
    rid = _start(sf, "empty")
    _confirm(sf, rid)

    assert delivery.calls == [], (
        "repo_apply ran on output that was never produced — retrying a tool "
        "cannot write the file the agent did not write")


def test_an_empty_step_is_re_asked_not_forwarded(tmp_path):
    """The whole defect: it advanced to the reviewer with nothing to review."""
    sf, _ = _sf(tmp_path, _graph("reask"))
    rid = _start(sf, "reask")
    _confirm(sf, rid)

    assert _step_row(sf, rid)["status"] == "pending"
    assert _node(sf, rid) == "impl"
    again = sf.claim_next_step(rid)
    assert again is not None and again.step_id == "impl", (
        "the reviewer must not be claimed for an implementation that does not exist")


def test_the_re_ask_says_what_was_missing(tmp_path):
    sf, _ = _sf(tmp_path, _graph("told"))
    rid = _start(sf, "told")
    _confirm(sf, rid)

    again = sf.claim_next_step(rid)
    assert "promoted no files" in str(again.validation_error), (
        "the retry lost the reason, so the agent repeats the same empty turn")


def test_the_skip_is_in_the_durable_trace(tmp_path):
    sf, _ = _sf(tmp_path, _graph("traced"))
    rid = _start(sf, "traced")
    _confirm(sf, rid)

    skipped = [t["payload"] for t in sf.get_trace(rid)
               if t["category"] == "lifecycle" and t["event"] == "on_deliver"
               and t["payload"].get("status") == "skipped"]
    assert skipped, "the skipped delivery left no mark on the trace"
    assert "Nothing to deliver" in skipped[0].get("detail", "")


def test_a_step_that_wrote_something_still_delivers(tmp_path):
    """The normal path is untouched: files staged → hook runs → step completes."""
    sf, delivery = _sf(tmp_path, _graph("normal"))
    rid = _start(sf, "normal")
    _confirm(sf, rid, {"main.py": "x = 1\n"})

    assert len(delivery.calls) == 1
    assert _step_row(sf, rid)["status"] == "completed"
    assert _node(sf, rid) == "review"


def test_a_real_delivery_failure_still_fails_the_step(tmp_path):
    """Only the empty case changed. A hook that fails on REAL output is still
    a permanent step failure — it is only reported better (see the trace test)."""
    sf, _ = _sf(tmp_path, _graph("realfail"))
    sf._tool_loader = MockToolLoader(
        {"repo_apply": lambda source_dir, **kw: {"applied": False, "files": [],
                                                 "error": "git commit failed: index.lock"}})
    rid = _start(sf, "realfail")
    _confirm(sf, rid, {"main.py": "x = 1\n"})

    row = _step_row(sf, rid)
    assert row["status"] == "failed"
    assert "index.lock" in row["last_error"]


def test_a_permanent_lifecycle_failure_is_traced_with_its_edge(tmp_path):
    """It used to leave NOTHING behind: the run read as "hook failed … next step
    claimed", with neither the step's death nor the `_error` edge it took."""
    sf, _ = _sf(tmp_path, _graph("edge"))
    sf._tool_loader = MockToolLoader(
        {"repo_apply": lambda source_dir, **kw: {"applied": False, "files": [],
                                                 "error": "git commit failed"}})
    rid = _start(sf, "edge")
    _confirm(sf, rid, {"main.py": "x = 1\n"})

    failed = [t["payload"] for t in sf.get_trace(rid)
              if t["category"] == "step" and t["event"] == "failed"]
    assert failed, "a step that never completed left no failure in the trace"
    assert failed[0]["routed_to"] == "review"


def test_exhausting_the_budget_fails_the_step(tmp_path):
    """Re-asking is bounded. When the agent stays silent, the step fails — the
    graph's own `_error` edge then decides, and it is traced on the way out."""
    sf, delivery = _sf(tmp_path, _graph("spent", max_retries=1))
    rid = _start(sf, "spent")
    _confirm(sf, rid)   # attempt 1 → re-ask
    _confirm(sf, rid)   # attempt 2 → budget spent

    assert delivery.calls == []
    assert _step_row(sf, rid)["status"] == "failed"
    assert _node(sf, rid) == "review", "the graph's `_error` escape still applies"


def test_without_an_error_edge_the_run_fails_rather_than_drifting(tmp_path):
    sf, _ = _sf(tmp_path, _graph("noedge", error_edge=False, max_retries=1))
    rid = _start(sf, "noedge")
    _confirm(sf, rid)
    _confirm(sf, rid)

    run = sf._conn.execute(
        "SELECT status FROM skillflow_runs WHERE id = ?", (rid,)).fetchone()
    assert run["status"] == "failed"


def test_an_empty_re_run_cannot_re_deliver_the_previous_attempt(tmp_path):
    """`_step_commit` leaves the prior `{step}/` in place when it promotes
    nothing, so a hook pointed at $STEP_DIR would have re-applied — and
    re-committed — output the current attempt never wrote."""
    sf, delivery = _sf(tmp_path, _graph("stale"))
    rid = _start(sf, "stale")
    step_dir = sf._workspace.get_step_dir("pid", "stale", "impl")
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / "from_a_previous_attempt.py").write_text("x = 1\n")

    _confirm(sf, rid)

    assert delivery.calls == []
