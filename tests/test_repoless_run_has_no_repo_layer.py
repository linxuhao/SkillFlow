"""A run that declares no code repository must not be given one.

`code_path_resolver` returning None means "no opinion, use your default", so a
host had no way to say "this project owns no repository at all". A repo-less run
therefore had `projects_base/<project_id>` invented for it, and `build_source_map`
attached that path as a `repo` read layer on nothing more than an `is_dir()`
check. Its repo access thus depended on whether that directory happened to
exist — an accident, not a decision, and the accident was already being made:
the directory was there (empty) in the run where this was found.

The resolver can now answer `False`. These tests pin the difference, and they
CREATE the directory with a file in it on purpose: a test that relied on the
path being absent would pass just as happily with the old behaviour.
"""

import pytest

from skillflow.core import SkillFlow
from skillflow.graph import (
    PipelineGraph, StepNode, Transition, EndConditions, EndCondition,
)
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager


def _graph() -> PipelineGraph:
    return PipelineGraph(
        name="g", begin="work",
        steps=[
            StepNode(id="work", step_type="agent", agent_config="worker",
                     context=[{"source": {"step": "prior"}}],
                     transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=EndConditions(combinator="or", conditions=[
            EndCondition(type="node_reached", node="done", result="completed")]))


def _engine(tmp_path, resolver):
    # The default path is POPULATED, so nothing here passes merely because a
    # directory is missing.
    d = tmp_path / "projects" / "p1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "IN_THE_DEFAULT_REPO.txt").write_text("x", encoding="utf-8")

    sf = SkillFlow(":memory:", tool_loader=ToolLoader(),
                   workspace_base=str(tmp_path / "workspaces"),
                   projects_base=str(tmp_path / "projects"),
                   code_path_resolver=resolver)
    sf.register_agent_config("worker", tools=["read_file"])
    sf.register_graph(_graph())
    return sf


def _listing(sf) -> str:
    run_id = sf.create_run("g", project_id="p1")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    sf.claim_next_step(run_id)
    return str(sf.execute_tool("list", {}, run_id=run_id, step_id="work"))


# ── the resolver contract ────────────────────────────────────────────────

def test_false_means_no_repo_and_none_still_means_no_opinion(tmp_path):
    (tmp_path / "projects" / "p1").mkdir(parents=True)
    ws = WorkspaceManager(base_path=str(tmp_path / "workspaces"),
                          projects_base=str(tmp_path / "projects"),
                          code_path_resolver=lambda pid: False)
    assert ws.get_project_code_path("p1") is None

    ws_default = WorkspaceManager(base_path=str(tmp_path / "workspaces"),
                                  projects_base=str(tmp_path / "projects"),
                                  code_path_resolver=lambda pid: None)
    assert ws_default.get_project_code_path("p1") == \
        (tmp_path / "projects" / "p1").resolve()

    ws_linked = WorkspaceManager(base_path=str(tmp_path / "workspaces"),
                                 projects_base=str(tmp_path / "projects"),
                                 code_path_resolver=lambda pid: str(tmp_path / "elsewhere"))
    assert ws_linked.get_project_code_path("p1") == (tmp_path / "elsewhere").resolve()


# ── what the step can actually read ──────────────────────────────────────

def test_a_repoless_run_gets_no_repo_read_layer(tmp_path):
    sf = _engine(tmp_path, resolver=lambda pid: False)
    assert "IN_THE_DEFAULT_REPO.txt" not in _listing(sf)


def test_the_control_proves_the_test_can_tell_the_difference(tmp_path):
    """Same populated directory, resolver merely has 'no opinion' — the default
    applies and the file IS readable. Without this, the test above would pass on
    a build that had simply broken the repo layer for everyone."""
    sf = _engine(tmp_path, resolver=lambda pid: None)
    assert "IN_THE_DEFAULT_REPO.txt" in _listing(sf)


def test_the_after_deliver_refusal_reaches_the_caller_as_a_readable_reason(tmp_path):
    """The hook's own comment: callers expect `error` (singular string).

    An `errors` list returns above the normalizer, and the consumer reads
    `hook_result.get("error", f"Lifecycle hook '{hook_name}' failed")` — so a
    plural key means the step fails with the generic sentence and the operator
    is told nothing about why.
    """
    sf = _engine(tmp_path, resolver=lambda pid: False)
    run_id = sf.create_run("g", project_id="p1")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    node = sf._get_resolver_for_run(run_id).get_node("work")

    result = sf._execute_check_hook(claimed.token, node, "after_deliver",
                                    [{"files": ["out.md"], "tool": "file_exists"}])

    assert result["passed"] is False
    # What the caller will actually surface, via its own .get(…, generic):
    surfaced = result.get("error", "Lifecycle hook 'after_deliver' failed")
    assert "declares none" in surfaced, surfaced


def test_a_repoless_after_deliver_still_honours_a_specs_own_on_failure(tmp_path):
    """`on_failure: "warn"` is honoured by StepValidator, not by the caller.

    The caller resolves the HOOK-level policy from `hook_spec`, and for
    after_deliver that is always a LIST — `isinstance(hook_spec, dict)` is
    False, so it resolves to "fail" and goes to `_fail_step_in_tx(
    retryable=False)`. Any single `{"passed": False}` returned above the
    validator therefore converts every warn-level after_deliver spec into a
    permanent step failure. dpe_default marks all of them "warn", with a comment
    saying exactly this.
    """
    sf = _engine(tmp_path, resolver=lambda pid: False)
    run_id = sf.create_run("g", project_id="p1")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    node = sf._get_resolver_for_run(run_id).get_node("work")

    result = sf._execute_check_hook(
        claimed.token, node, "after_deliver",
        [{"files": ["**/*.py"], "tool": "file_exists", "on_failure": "warn"}])

    assert result["passed"] is True, \
        "a warn-only after_deliver became a hard, unretryable step failure"
    # …and the reason is still said out loud, on the channel the caller reads
    # for warn-level results.
    assert any("declares none" in w.get("error", "")
               for w in result.get("warnings", [])), result


def test_a_repoless_tool_step_is_handed_no_project_root_at_all(tmp_path):
    """Not "" — `Path("").resolve()` is the process CWD.

    repo_apply does `dst = Path(project_root).resolve()` and then copies + `git
    add -A` + commits into it; git_sync_pre and repo_validate resolve the same
    value. An empty root therefore aims those at whatever directory the server
    process happens to run in, which in a container is its own checkout.

    The probe takes `**kw` on purpose: one declaring `project_root=""` cannot
    tell "handed an empty string" from "handed nothing and using its default",
    which is the whole distinction under test.
    """
    seen = {}

    def probe(**kw):
        seen.update(kw)
        seen["_keys"] = set(kw)
        return {"passed": True}

    sf = _engine(tmp_path, resolver=lambda pid: False)
    sf._tool_loader.register_dynamic_tool(
        "probe", {"name": "probe", "description": "records its kwargs"}, probe)
    sf.register_graph(PipelineGraph(
        name="withtool", begin="t",
        steps=[
            StepNode(id="t", step_type="tool", tool_name="probe",
                     transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=EndConditions(combinator="or", conditions=[
            EndCondition(type="node_reached", node="done", result="completed")])))

    run_id = sf.create_run("withtool", project_id="p1")
    sf.start_run(run_id)
    sf.advance_run(run_id)

    assert "workspace_root" in seen["_keys"], \
        "the probe never ran through the injection path; this proves nothing"
    assert "project_root" not in seen["_keys"], \
        f"tool step received project_root={seen.get('project_root')!r}"


def test_a_repoless_on_deliver_hook_is_handed_no_project_root_either(tmp_path):
    """The lifecycle-hook path fills the same argument from the same source.

    This is the path `on_deliver: repo_apply` actually takes, so it is the one
    that would have copied a repo-less run's step output into the server's own
    checkout and committed it.

    A sentinel default rather than `**kw`: this path filters on
    `k in sig.parameters` and does NOT pass through to a **kwargs-only tool, so
    a `**kw` probe would receive nothing and prove nothing. The sentinel still
    separates "handed an empty string" from "handed nothing".
    """
    missing = object()
    seen = {}

    def probe(workspace_root="", project_root=missing, **_kw):
        seen["workspace_root"] = workspace_root
        seen["project_root"] = project_root
        return {"passed": True}

    sf = _engine(tmp_path, resolver=lambda pid: False)
    sf._tool_loader.register_dynamic_tool(
        "probe", {"name": "probe", "description": "records its kwargs"}, probe)
    run_id = sf.create_run("g", project_id="p1")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    node = sf._get_resolver_for_run(run_id).get_node("work")

    sf._execute_lifecycle_hook(claimed.token, node, "on_deliver",
                               {"tool": "probe"})

    assert seen.get("workspace_root"), \
        "the hook never ran through the injection path; this proves nothing"
    assert seen["project_root"] is missing, \
        f"on_deliver hook received project_root={seen['project_root']!r}"


def test_a_repoless_run_gets_no_repository_context_layer(tmp_path):
    """`from: repository` must not be answered with the project BRIEF dir.

    ContextResolver falls back to `workspace_root/"project"` when code_root is
    None — and that directory exists and is populated, so the fallback is worse
    than the empty `projects_base/<id>` it replaced: the brief is served to the
    agent labelled "Repository".
    """
    from skillflow.graph import _normalize_context_spec

    sf = _engine(tmp_path, resolver=lambda pid: False)
    ws = sf._workspace.get_project_path("p1")
    brief = ws / "project"
    brief.mkdir(parents=True, exist_ok=True)
    (brief / "brief.md").write_text("THE BRIEF, NOT A REPO", encoding="utf-8")

    sf.register_graph(PipelineGraph(
        name="withrepo", begin="work",
        steps=[
            StepNode(id="work", step_type="agent", agent_config="worker",
                     context=[_normalize_context_spec(
                         {"from": "repository", "path": "brief.md"})],
                     transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=EndConditions(combinator="or", conditions=[
            EndCondition(type="node_reached", node="done", result="completed")])))

    run_id = sf.create_run("withrepo", project_id="p1")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)

    resolved = claimed.inputs.get("_resolved_context", {})
    assert "THE BRIEF, NOT A REPO" not in str(resolved), \
        "the project brief dir was served as the repository"


def test_the_context_control_proves_a_real_repo_is_still_served(tmp_path):
    """Same spec, resolver merely has 'no opinion' — the legacy fallback applies
    and the file IS injected. Without this, the test above would pass on a build
    that had simply broken `from: repository` for every run."""
    from skillflow.graph import _normalize_context_spec

    sf = _engine(tmp_path, resolver=lambda pid: None)
    (tmp_path / "projects" / "p1" / "brief.md").write_text(
        "IN THE REAL REPO", encoding="utf-8")

    sf.register_graph(PipelineGraph(
        name="withrepo", begin="work",
        steps=[
            StepNode(id="work", step_type="agent", agent_config="worker",
                     context=[_normalize_context_spec(
                         {"from": "repository", "path": "brief.md"})],
                     transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=EndConditions(combinator="or", conditions=[
            EndCondition(type="node_reached", node="done", result="completed")])))

    run_id = sf.create_run("withrepo", project_id="p1")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)

    assert "IN THE REAL REPO" in str(claimed.inputs.get("_resolved_context", {}))
