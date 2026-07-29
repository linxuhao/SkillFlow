"""A run that dies on a routing decision must report the REASON, not the EDGE.

Observed on a real novel chapter (2026-07-29): the run's `error_reason` read

    Cycle limit exceeded

while the fact that explains it was sitting in the run's own workspace, in the
very file the matcher had just read to decide the edge:

    continuity/continuity_report.json
    {"passed": false, "violations": ["字数超限: 5662 字（上限 4500）"]}

The engine had the reason and discarded it. Same shape for "No matching
transition from 'X' with flags {...}" — it prints the flags and not the content
of the file the edges route on.

Letting `max_loop` exhaust IS a legitimate terminal (17 of 22 host configs rely
on it, including every dpe_default review loop). Only the message was wrong.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skillflow.core import StepResult, _routing_reason
from skillflow.graph import PipelineGraph


# ── The extractor, in isolation ───────────────────────────────────────────

class _T:
    def __init__(self, match):
        self.match = match


class _N:
    def __init__(self, *matches):
        self.transitions = [_T(m) for m in matches]


def _reader(files: dict):
    def read(path: str) -> str:
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]
    return read


class TestItReadsTheFileTheEdgesRouteOn:
    def test_the_live_case_violations_reach_the_message(self):
        node = _N({"from_file": "continuity/continuity_report.json",
                   "field": "passed", "value": True})
        out = _routing_reason(node, _reader({
            "continuity/continuity_report.json": json.dumps(
                {"passed": False, "violations": ["字数超限: 5662 字（上限 4500）"]},
                ensure_ascii=False)}))
        assert "字数超限: 5662 字（上限 4500）" in out
        assert "continuity/continuity_report.json" in out

    def test_feedback_wins_over_the_other_fields(self):
        node = _N({"from_file": "v.json", "field": "passed", "value": True})
        out = _routing_reason(node, _reader({
            "v.json": '{"passed": false, "summary": "s", "feedback": "the why"}'}))
        assert out == "v.json feedback: the why"

    def test_every_routing_file_is_reported(self):
        node = _N({"from_file": "a.json", "field": "ok", "value": True},
                  {"from_file": "b.json", "field": "ok", "value": False})
        out = _routing_reason(node, _reader({
            "a.json": '{"error": "first"}', "b.json": '{"error": "second"}'}))
        assert "first" in out and "second" in out

    def test_a_repeated_path_is_read_once(self):
        node = _N({"from_file": "v.json", "field": "passed", "value": True},
                  {"from_file": "v.json", "field": "passed", "value": False})
        out = _routing_reason(node, _reader({"v.json": '{"error": "boom"}'}))
        assert out.count("boom") == 1

    def test_a_multiline_reason_stays_one_line(self):
        node = _N({"from_file": "v.json", "field": "passed", "value": True})
        out = _routing_reason(node, _reader({
            "v.json": json.dumps({"error": "line one\n  line two"})}))
        assert "\n" not in out and "line one line two" in out


class TestItIsBounded:
    def test_a_huge_field_is_truncated(self):
        node = _N({"from_file": "v.json", "field": "passed", "value": True})
        out = _routing_reason(node, _reader({
            "v.json": json.dumps({"violations": ["x" * 200] * 50})}))
        assert len(out) <= 300

    def test_only_the_first_files_are_read(self):
        reads = []

        def read(path):
            reads.append(path)
            return '{"error": "e"}'
        node = _N(*[{"from_file": f"{i}.json", "field": "p", "value": True}
                    for i in range(10)])
        _routing_reason(node, read)
        assert len(reads) == 3


class TestItNeverTurnsAFailureIntoACrash:
    def test_a_missing_file_is_silent(self):
        node = _N({"from_file": "gone.json", "field": "p", "value": True})
        assert _routing_reason(node, _reader({})) == ""

    def test_unparseable_content_is_silent(self):
        node = _N({"from_file": "v.json", "field": "p", "value": True})
        assert _routing_reason(node, _reader({"v.json": "not json {{{"})) == ""

    def test_a_reader_that_explodes_is_silent(self):
        def boom(path):
            raise RuntimeError("workspace gone")
        node = _N({"from_file": "v.json", "field": "p", "value": True})
        assert _routing_reason(node, boom) == ""

    def test_no_from_file_edges_add_nothing(self):
        assert _routing_reason(_N({"passed": True}), _reader({})) == ""
        assert _routing_reason(_N(None), _reader({})) == ""

    def test_no_node_and_no_reader_are_tolerated(self):
        assert _routing_reason(None, _reader({})) == ""
        assert _routing_reason(_N({"from_file": "v.json"}), None) == ""

    def test_a_json_file_with_nothing_human_readable_adds_nothing(self):
        node = _N({"from_file": "v.json", "field": "passed", "value": True})
        assert _routing_reason(node, _reader({"v.json": '{"passed": false}'})) == ""

    def test_a_null_field_does_not_shadow_the_real_reason(self):
        """`{"error": null}` is how a checker says "no error". Printing it as the
        literal "None" both said nothing AND stopped the search one key short of
        the violations — the discard this whole change exists to stop."""
        node = _N({"from_file": "v.json", "field": "passed", "value": True})
        out = _routing_reason(node, _reader({"v.json": json.dumps(
            {"passed": False, "error": None, "feedback": None,
             "violations": ["字数超限: 5662 字"]}, ensure_ascii=False)}))
        assert out == "v.json violations: 字数超限: 5662 字"

    def test_an_all_null_file_adds_nothing(self):
        node = _N({"from_file": "v.json", "field": "passed", "value": True})
        assert _routing_reason(node, _reader({
            "v.json": '{"passed": false, "error": null, "summary": null}'})) == ""


# ── End to end: an exhausted review loop ──────────────────────────────────

def _review_loop_graph(max_loop=2):
    return PipelineGraph._from_dict({
        "name": "g", "description": "x", "begin": "write",
        "steps": [
            {"id": "write", "step_type": "agent", "agent_config": "maker",
             "output": {"mode": "write"}, "transitions": [{"to": "check"}]},
            {"id": "check", "step_type": "agent", "agent_config": "reviewer",
             "output": {"mode": "write"},
             "transitions": [
                 {"to": "done",
                  "match": {"from_file": "continuity_report.json",
                            "field": "passed", "value": True}},
                 {"to": "write",
                  "match": {"from_file": "continuity_report.json",
                            "field": "passed", "value": False},
                  "max_loop": max_loop},
             ]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    })


def _drive(sf, rid, verdict: dict, limit=30):
    """Run until terminal, the reviewer always writing the same verdict."""
    for _ in range(limit):
        if sf.get_run(rid)["status"] in ("completed", "failed"):
            break
        sf.advance_run(rid)
        if sf.get_run(rid)["status"] in ("completed", "failed"):
            break
        claimed = sf.claim_next_step(rid)
        if claimed is None:
            continue
        tmp = sf._workspace.get_step_tmp_dir("p", "g", claimed.step_id)
        if claimed.step_id == "write":
            (tmp / "chapter.md").write_text("...")
        else:
            (tmp / "continuity_report.json").write_text(
                json.dumps(verdict, ensure_ascii=False))
        sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))
    return sf.get_run(rid)


def test_an_exhausted_review_loop_reports_the_violation(sf_with_workspace):
    sf = sf_with_workspace
    for role in ("maker", "reviewer"):
        sf.register_agent_config(role, model="mock", tools=[])
    sf.register_graph(_review_loop_graph())
    rid = sf.get_or_create_run("g", "p", {"project_id": "p"})
    sf.start_run(rid)

    run = _drive(sf, rid, {"passed": False,
                           "violations": ["字数超限: 5662 字（上限 4500）"]})

    assert run["status"] == "failed"
    assert "Cycle limit exceeded" in run["error_reason"]      # the edge, still
    assert "字数超限: 5662 字（上限 4500）" in run["error_reason"]   # and now the why
    # …and EARLY: the host that prompted this renders a 160-char slice of the
    # reason as the project's status chip. The exhausted-edge list alone runs
    # ~150 chars on real step ids, so a reason appended after it is a reason
    # nobody sees.
    assert run["error_reason"].index("字数超限") < 160


def test_an_unmatched_transition_reports_the_file_it_routed_on(sf_with_workspace):
    """No edge matches at all — the verdict is neither true nor false."""
    sf = sf_with_workspace
    for role in ("maker", "reviewer"):
        sf.register_agent_config(role, model="mock", tools=[])
    sf.register_graph(_review_loop_graph())
    rid = sf.get_or_create_run("g", "p", {"project_id": "p"})
    sf.start_run(rid)

    run = _drive(sf, rid, {"passed": "unknown", "error": "reviewer crashed"})

    assert run["status"] == "failed"
    assert "No matching transition" in run["error_reason"]
    assert "reviewer crashed" in run["error_reason"]


def _seed(sf, step_id, payload):
    d = sf._workspace.get_step_dir("p", "g", step_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "continuity_report.json").write_text(
        json.dumps(payload, ensure_ascii=False))


def test_an_exhausted_tool_gate_reports_the_violation(sf_with_workspace):
    """The checker is a TOOL step — `_complete_tool_step` resolves its edges."""
    sf = sf_with_workspace
    sf.register_agent_config("maker", model="mock", tools=[])
    sf._tool_loader.register("continuity_check", lambda **kw: {"passed": False})
    sf.register_graph(PipelineGraph._from_dict({
        "name": "g", "description": "x", "begin": "write",
        "steps": [
            {"id": "write", "step_type": "agent", "agent_config": "maker",
             "output": {"mode": "write"}, "transitions": [{"to": "check"}]},
            {"id": "check", "step_type": "tool", "tool_name": "continuity_check",
             "transitions": [
                 {"to": "done", "match": {"from_file": "continuity_report.json",
                                          "field": "passed", "value": True}},
                 {"to": "write", "match": {"from_file": "continuity_report.json",
                                           "field": "passed", "value": False},
                  "max_loop": 2},
             ]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }))
    rid = sf.get_or_create_run("g", "p", {"project_id": "p"})
    sf.start_run(rid)
    _seed(sf, "check", {"passed": False, "violations": ["字数超限: 5662 字"]})

    run = _drive(sf, rid, {})
    assert run["status"] == "failed"
    assert "Cycle limit exceeded" in run["error_reason"]
    assert "字数超限: 5662 字" in run["error_reason"]


def test_an_unmatched_gate_reports_the_file_it_routed_on(sf_with_workspace):
    """A GATE that matches nothing — same discard, same fix."""
    sf = sf_with_workspace
    sf.register_agent_config("reviewer", model="mock", tools=[])
    sf.register_graph(PipelineGraph._from_dict({
        "name": "g", "description": "x", "begin": "check",
        "steps": [
            {"id": "check", "step_type": "agent", "agent_config": "reviewer",
             "output": {"mode": "write"}, "transitions": [{"to": "route"}]},
            {"id": "route", "step_type": "gate", "transitions": [
                {"to": "done", "match": {"from_file": "continuity_report.json",
                                         "field": "passed", "value": True}}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }))
    rid = sf.get_or_create_run("g", "p", {"project_id": "p"})
    sf.start_run(rid)

    run = _drive(sf, rid, {"passed": False, "violations": ["缺少伏笔回收"]})
    assert run["status"] == "failed"
    assert "no matching transition" in run["error_reason"]
    assert "缺少伏笔回收" in run["error_reason"]


def _gate_graph(sf):
    sf.register_agent_config("reviewer", model="mock", tools=[])
    sf.register_graph(PipelineGraph._from_dict({
        "name": "g", "description": "x", "begin": "check",
        "steps": [
            {"id": "check", "step_type": "agent", "agent_config": "reviewer",
             "output": {"mode": "write"}, "transitions": [{"to": "route"}]},
            {"id": "route", "step_type": "gate", "transitions": [
                {"to": "done", "match": {"from_file": "continuity_report.json",
                                         "field": "passed", "value": True}}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }))
    rid = sf.get_or_create_run("g", "p", {"project_id": "p"})
    sf.start_run(rid)
    return rid


def _park_on(sf, rid, node):
    """Leave the run pre-resolved AT a node — the state a tick sees after a
    restart, which advance_run handles in a separate branch from the main loop."""
    with sf._tx() as conn:
        conn.execute("UPDATE skillflow_runs SET current_node = ? WHERE id = ?",
                     (node, rid))


def test_a_pre_resolved_gate_can_match_on_a_file(sf_with_workspace):
    """A gate reached with current_node already set resolved with NO file_reader,
    so a `from_file` edge could never match: the same gate routed through the main
    loop and dead-ended when a tick found it pre-resolved."""
    sf = sf_with_workspace
    rid = _gate_graph(sf)
    sf.advance_run(rid)
    c = sf.claim_next_step(rid)
    (sf._workspace.get_step_tmp_dir("p", "g", c.step_id)
     / "continuity_report.json").write_text(json.dumps({"passed": True}))
    sf.confirm_step(c.token, StepResult(outputs={}, flags={}))
    _park_on(sf, rid, "route")

    sf.advance_run(rid)
    assert sf.get_run(rid)["status"] == "completed"


def test_a_pre_resolved_gate_that_matches_nothing_says_why(sf_with_workspace):
    sf = sf_with_workspace
    rid = _gate_graph(sf)
    sf.advance_run(rid)
    c = sf.claim_next_step(rid)
    (sf._workspace.get_step_tmp_dir("p", "g", c.step_id)
     / "continuity_report.json").write_text(json.dumps(
         {"passed": False, "violations": ["缺少伏笔回收"]}, ensure_ascii=False))
    sf.confirm_step(c.token, StepResult(outputs={}, flags={}))
    _park_on(sf, rid, "route")

    sf.advance_run(rid)
    run = sf.get_run(rid)
    assert run["status"] == "failed"
    assert "no matching transition" in run["error_reason"]
    assert "缺少伏笔回收" in run["error_reason"]


def test_a_clean_pass_is_unaffected(sf_with_workspace):
    sf = sf_with_workspace
    for role in ("maker", "reviewer"):
        sf.register_agent_config(role, model="mock", tools=[])
    sf.register_graph(_review_loop_graph())
    rid = sf.get_or_create_run("g", "p", {"project_id": "p"})
    sf.start_run(rid)

    run = _drive(sf, rid, {"passed": True})

    assert run["status"] == "completed"
    assert not run["error_reason"]
