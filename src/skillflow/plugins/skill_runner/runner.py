"""SkillTool — stateful callable wrapping a skillflow pipeline as an agent tool.

Usage::

    sf = SkillFlow(":memory:", tool_loader=loader)
    tool = SkillTool(sf, "skill_review")
    tool.register_agent("analyst", model="...", tools=["read_file", "grep"])

    # Agent loop:
    resp = tool(action="next")
    while resp.status == "in_progress":
        # agent does work...
        resp = tool(action="submit", result={"findings": [...]})
    # resp.status == "completed" or "failed"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from skillflow.exceptions import SkillFlowError
from skillflow.core import SkillFlow, StepResult, ClaimedStep
from skillflow.exceptions import IsolationUnavailable


@dataclass
class SkillResponse:
    """Returned by SkillTool to instruct the agent what to do next."""

    status: str  # "in_progress" | "paused" | "approved" | "completed" | "failed"
    run_id: str = ""
    step: str = ""
    instruction: str = ""
    tools: dict[str, dict] = field(default_factory=dict)
    # Tool node delegation (when SkillFlow.delegate_tools_to_agent is True)
    tool_name: str = ""
    tool_params: dict = field(default_factory=dict)
    checkpoint_label: str = ""
    checkpoint_reject_to: str = ""
    expected_checkpoint: dict = field(default_factory=dict)
    decision_id: str = ""
    outputs: dict = field(default_factory=dict)
    error: str = ""
    steps_completed: int = 0
    # Output file contract — where to write fixed-output files before submit
    output_dir: str = ""
    output_target: str = "artifact"
    output_targets: dict[str, str] = field(default_factory=dict)
    artifact_dir: str = ""
    expected_files: list[str] = field(default_factory=list)
    validation_error: str = ""


class SlotRejected(ValueError):
    """A submitted output slot was refused by its write tool."""


_OVERVIEW_MAX_STEPS = 30


def _fmt(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _describe_edge(t) -> str:
    """One outgoing transition as short prose: target plus its condition.

    Mirrors graph._flags_match: a from_file match reads the step's own output
    file; a field/value match and a direct key match read the submitted
    result's top-level keys.
    """
    target = f"`{t.to}`" if t.to else "end"
    match = dict(t.match or {})
    if "from_file" in match and "field" in match and "value" in match:
        cond = (f"if {match['field']}={_fmt(match['value'])} in your "
                f"`{match['from_file']}`")
    elif "field" in match and "value" in match:
        cond = f"if {match['field']}={_fmt(match['value'])}"
    elif match.get("from") == "checkpoint":
        cond = f"when {match.get('value', 'approved')}"
    elif match:
        cond = "if " + ", ".join(f"{k}={_fmt(v)}" for k, v in match.items())
    else:
        cond = ""
    loop = f" (at most {t.max_loop} times)" if t.max_loop else ""
    label = f' "{t.label}"' if t.label else ""
    return " ".join(p for p in (target, cond) if p) + loop + label


def pipeline_overview(graph, current_step: str, done: dict[str, int], *,
                      tools_delegated: bool = True) -> str:
    """Whole-pipeline map shown before every step instruction.

    A driving agent that only ever sees the current step cannot pace itself
    or anticipate a review loop or checkpoint; showing every step first and
    then the current one measured better (agent-depth research, 2026-10).
    Built from the run's pinned graph; ``done`` counts completed executions
    per step id. ``tools_delegated`` says whether tool steps come to the
    agent or run inside the engine. Large graphs are windowed around the
    current step.
    """
    steps = list(getattr(graph, "steps", []) or [])
    if not steps:
        return ""
    lines = []
    for i, node in enumerate(steps, 1):
        kind = node.step_type
        if kind == "tool":
            who = "you run it" if tools_delegated else "automatic"
            kind = f"tool `{node.tool_name}` ({who})"
        elif kind == "gate":
            kind = "gate (automatic)"
        elif kind == "loop" and node.loop is not None:
            src = node.loop.source or {}
            where = "/".join(str(src[k]) for k in ("step", "file") if src.get(k))
            kind = (f"loop (automatic) over items{f' of `{where}`' if where else ''}, "
                    f"at most {node.loop.max_iterations}")
        if node.checkpoint:
            label = f': "{node.checkpoint_label}"' if node.checkpoint_label else ""
            back = (f"; a rejection returns to `{node.checkpoint_reject_to}`"
                    if node.checkpoint_reject_to else "")
            kind += f", human checkpoint{label}{back}"
        edges = "; ".join(_describe_edge(t) for t in node.transitions) or "end"
        mark = "▶ " if node.id == current_step else ""
        count = done.get(node.id, 0)
        done_note = f" [done ×{count}]" if count else ""
        lines.append(f"{i}. {mark}`{node.id}` — {kind} → {edges}{done_note}")

    if len(lines) > _OVERVIEW_MAX_STEPS:
        cur = next((i for i, n in enumerate(steps) if n.id == current_step), 0)
        keep = set(range(3)) | set(range(len(lines) - 2, len(lines)))
        keep |= set(range(max(0, cur - 10), min(len(lines), cur + 11)))
        shown, prev = [], -1
        for i in sorted(keep):
            if i != prev + 1:
                shown.append(f"   … {i - prev - 1} more steps …")
            shown.append(lines[i])
            prev = i
        lines = shown

    desc = (graph.description or "").strip().replace("\n", " ")
    if len(desc) > 200:
        desc = desc[:197] + "..."
    head = f"`{graph.name}`" + (f" — {desc}" if desc else "")
    pos = next((i for i, n in enumerate(steps, 1) if n.id == current_step), None)
    where = (f"You are on step {pos} of {len(steps)} (`{current_step}`)"
             if pos else f"Current step: `{current_step}`")
    total_done = sum(done.values())
    return (f"## Pipeline overview\n\n{head}\n{where}; completed step "
            f"executions so far: {total_done}. The engine decides the next "
            f"step; this map is for planning only.\n\n" + "\n".join(lines) + "\n")


class PromptAssembler:
    """Turns a ClaimedStep into an agent-facing instruction string.

    ``include_overview``: SkillTool prefixes each instruction with the
    whole-pipeline overview; a subclass that formats its own prompt can set
    it False.

    Override ``assemble()`` to customize prompt formatting.
    """

    include_overview = True

    def assemble(self, step: ClaimedStep) -> str:
        """Build the instruction from step context and agent config."""
        parts: list[str] = []
        inputs = step.inputs

        # Human-readable step label (from snake_case id)
        label = step.step_id.replace("_", " ").title()

        # Validation error from a previous submit failure
        validation_error = inputs.get("_validation_error")
        if validation_error:
            parts.append(f"## Validation failed\n\n{validation_error}\n")

        # Feedback from a rejected checkpoint or failed lifecycle hook
        feedback = inputs.get("_feedback")
        if feedback:
            parts.append(f"## Feedback from previous attempt\n\n{feedback}\n")

        # Error context from a previous failure (error_handler routing)
        error_ctx = inputs.get("_error")
        if error_ctx:
            parts.append(f"## Previous error\n\n{error_ctx}\n")

        # Resolved context from graph context specs
        resolved = inputs.get("_resolved_context", {})
        if resolved:
            parts.append("## Context\n")
            for key, val in resolved.items():
                parts.append(f"### {key}\n\n{val}\n")

        # Agent config system prompt (if provided)
        agent_cfg = inputs.get("_agent_config", {})
        system_prompt = agent_cfg.get("config", {}).get("system_prompt", "")
        if system_prompt:
            parts.append(f"## Role\n\n{system_prompt}\n")

        # The step task itself
        parts.append(f"## Task: {label}\n\nExecute step `{step.step_id}`.")

        # Output contract — transport-neutral. Naming write_plan/create_plan
        # as directly-callable functions here misled every driving agent whose
        # host doesn't expose them (they exist natively only for
        # scheduler-spawned agents); instead we describe the SLOTS and the two
        # delivery paths that work on every transport (submit result / the
        # host's skillflow tool proxy).
        expected_files = inputs.get("_expected_files", [])
        tool_schemas = inputs.get("_tool_schemas", {})
        slots = self._slot_contract(expected_files, tool_schemas)
        if slots:
            parts.append("Expected outputs for this step:\n" + "\n".join(slots))
            parts.append(
                "Deliver each output EITHER by including it in your submit "
                "call — result={\"<slot>\": <content>} — OR, if your host "
                "exposes skillflow's step tools (e.g. a `skillflow_tool` "
                "proxy), by writing it with `write_<slot>` before submitting. "
                "Do not try to call write_/create_ tools that your host does "
                "not expose, and do not write these files with your own file "
                "tools — use the engine so each slot reaches its declared artifact/code destination."
            )

        parts.append("Produce the expected output in the format specified, "
                     "then submit to advance.")

        return "\n\n".join(parts)

    @staticmethod
    def _slot_contract(expected_files: list, tool_schemas: dict) -> list[str]:
        """Build '- slot → file: format hint' lines from write-tool schemas."""
        lines: list[str] = []
        seen: set[str] = set()
        for name, schema in (tool_schemas or {}).items():
            if not name.startswith("write_"):
                continue
            slot = name[len("write_"):]
            desc = (schema.get("description") or "").strip()
            # Keep the format hint ("Expected format: ...") — drop the
            # tool-invocation phrasing ("Replace X with new content.").
            hint = ""
            if "Expected format:" in desc:
                hint = " — " + desc[desc.index("Expected format:"):].strip()
            fname = ""
            for f in expected_files or []:
                stem = f.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if stem == slot or slot in f:
                    fname = f
                    break
            lines.append(f"- `{slot}`" + (f" → `{fname}`" if fname else "") + hint)
            seen.add(slot)
        # Files with no matching write-tool (rare) still get named.
        for f in expected_files or []:
            stem = f.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if stem not in seen and not any(f in ln for ln in lines):
                lines.append(f"- `{f}`")
        return lines


class SkillTool:
    """Stateful callable that wraps a skillflow pipeline as an agent tool.

    The host agent calls this like a tool function with:
    - action="next"     — start or advance the pipeline, get next instruction
    - action="submit"   — confirm the current step with result
    - action="approve"  — approve a paused checkpoint
    - action="reject"   — reject a checkpoint with feedback

    Parameters:
        sf: Configured SkillFlow instance (with registered graphs + agent configs).
        graph_name: Name of the registered graph to run.
        prompt_assembler: Custom PromptAssembler for instruction formatting.
    """

    def __init__(self, sf: SkillFlow, graph_name: str, *,
                 prompt_assembler: PromptAssembler | None = None,
                 project_id: str | None = None):
        self.sf = sf
        self.graph_name = graph_name
        self.run_id: str | None = None
        self._current_claim: ClaimedStep | None = None
        self._assembler = prompt_assembler or PromptAssembler()
        self._project_id = project_id

    # ── public API ─────────────────────────────────────────────────

    def register_agent(self, name: str, **kwargs):
        """Register a mock/default agent config referenced by the graph."""
        self.sf.register_agent_config(name, **kwargs)

    def __call__(self, action: str = "next", step_id: str = "",
                 result: dict | None = None,
                 feedback: str = "",
                 redirect_to: str = "",
                 run_id: str = "", expected_checkpoint: dict | None = None,
                 decision_id: str = "") -> SkillResponse:
        """Execute an action and return the next instruction.

        Args:
            action: "next" | "submit" | "approve" | "reject" | "abort"
            step_id: Required for "submit" and "reject" (to identify the step).
            result: Output dict for "submit".
            feedback: Rejection reason for "reject".
            redirect_to: Step ID to redirect to on reject (optional, from SkillResponse.checkpoint_reject_to).
            run_id: Resume an existing run. If empty and action is "next", a new run is created.

        Returns:
            SkillResponse with the next instruction or termination status.
        """
        # Approval replay must never reset a claim owned by the next executor.
        if action == "approve":
            rid = run_id or self.run_id or ""
            try:
                next_node = self.sf.approve_checkpoint(rid,
                    expected_checkpoint=expected_checkpoint, decision_id=decision_id)
            except SkillFlowError as exc:
                return SkillResponse(status="failed", run_id=rid, error=str(exc))
            return SkillResponse(status="approved", run_id=rid, step=next_node,
                                 expected_checkpoint=expected_checkpoint, decision_id=decision_id)

        # ── Reconnect to existing run if run_id provided ──────────
        if run_id and self.run_id is None:
            run = self.sf.get_run(run_id)
            if run is None:
                return SkillResponse(status="failed", error=f"Run not found: {run_id}")
            self.run_id = run_id
            self.graph_name = run.get("graph_name", self.graph_name)
            # Traversal recovers only this run and respects owner liveness.
            self.sf.advance_run(run_id)
            self._current_claim = (self.sf._owned_claim(run_id)
                                   or self.sf.claim_next_step(run_id))
            if (self._current_claim is None and run.get("status") == "running" and any(
                    step["status"] == "claimed" for step in self.sf.get_steps(run_id))):
                return SkillResponse(status="failed", run_id=run_id,
                                     error="Run claim is busy: owner is live or unknown")

        # ── Handle action ─────────────────────────────────────────
        if action == "abort":
            if self.run_id is not None:
                try:
                    self.sf.fail_run(self.run_id, "aborted by agent")
                except Exception:
                    # If swallowed, the abort is silently ignored and the run
                    # keeps looking alive.
                    import logging
                    logging.getLogger("skillflow").warning(
                        "fail_run failed while aborting %s", self.run_id,
                        exc_info=True)
            self.run_id = None
            self._current_claim = None
            return SkillResponse(status="aborted", run_id=self.run_id or "")

        if action == "next" and self.run_id is None:
            pid = self._project_id
            # Auto-generate project_id when workspace is configured
            if pid is None and self.sf._workspace is not None:
                import uuid
                pid = f"skill-{uuid.uuid4().hex[:8]}"
            self.run_id = self.sf.create_run(self.graph_name,
                                              project_id=pid)
            self.sf.start_run(self.run_id)

        elif action == "submit" and self._current_claim is not None:
            outputs = result or {}
            flags = outputs  # result dict doubles as flags for gate resolution
            self.sf.confirm_step(
                self._current_claim.token,
                StepResult(outputs=outputs, flags=flags),
            )
            self._current_claim = None

        elif action == "reject" and self.run_id is not None:
            run = self.sf.get_run(self.run_id)
            if run and run.get("status") == "paused":
                # Find the checkpoint step (last completed step)
                if not step_id:
                    steps = self.sf.get_steps(self.run_id)
                    completed = [s for s in steps if s.get("status") == "completed"]
                    if completed:
                        step_id = completed[-1]["step_id"]
                self.sf.reject_checkpoint(
                    self.run_id, step_id or "",
                    feedback or "Rejected",
                    redirect_to=redirect_to,
                )
                self._current_claim = None

        # ── Advance and respond ───────────────────────────────────
        return self._advance_and_respond()

    # ── Helpers ───────────────────────────────────────────────────

    def _make_response(self, claimed: ClaimedStep) -> SkillResponse:
        """Build a SkillResponse from a claimed step."""
        tool_name = ""
        tool_params = {}
        try:
            resolver = self.sf._get_resolver_for_run(self.run_id)
            node = resolver.get_node(claimed.step_id)
            if node and node.step_type == "tool":
                tool_name = node.tool_name
                tool_params = dict(node.tool_params)
                # Resolve path variables ($CONFIG_DIR, $STEP_DIR, $STEP_TMP_DIR,
                # $TASK_DIR, $PROJECT_ROOT) so the driving agent receives real
                # paths, not literal placeholders.  AGENT.md promises these are
                # resolved; without this, delegated tool nodes (e.g. a lint step
                # pointed at "$CONFIG_DIR/.../foo.yaml") get an unexpanded path.
                if self.sf._workspace is not None and self.run_id:
                    pid = self.sf._get_project_id(self.run_id)
                    gname = self.sf._get_graph_name(self.run_id) or self.graph_name
                    tool_params = self.sf._workspace.resolve_variables(
                        pid, gname, claimed.step_id, tool_params,
                        run_id=self.run_id)
        except IsolationUnavailable:
            # A run whose declared root cannot be resolved must not be handed a
            # tool call at all; unexpanded placeholders are the lesser bug.
            raise
        except Exception:
            # A swallow here leaves $CONFIG_DIR/$STEP_DIR/… unexpanded, so the
            # delegated tool runs against literal placeholder paths.
            import logging
            logging.getLogger("skillflow").warning(
                "path-variable resolution failed for delegated tool params",
                exc_info=True)

        instruction = self._assembler.assemble(claimed)
        overview = (self._overview(claimed.step_id)
                    if getattr(self._assembler, "include_overview", True) else "")
        if overview:
            instruction = overview + "\n" + instruction

        return SkillResponse(
            status="in_progress",
            run_id=self.run_id or "",
            step=claimed.step_id,
            instruction=instruction,
            tools=claimed.inputs.get("_tool_schemas", {}),
            tool_name=tool_name,
            tool_params=tool_params,
            output_dir=claimed.inputs.get("_output_dir", ""),
            output_target=claimed.inputs.get("_output_target", "artifact"),
            output_targets=claimed.inputs.get("_output_targets", {}),
            artifact_dir=claimed.inputs.get("_artifact_dir", ""),
            expected_files=claimed.inputs.get("_expected_files", []),
            validation_error=claimed.validation_error or claimed.inputs.get("_validation_error", ""),
        )

    def _overview(self, step_id: str) -> str:
        """Pipeline overview for this run's pinned graph ("" if unavailable)."""
        if not self.run_id:
            return ""
        graph = self.sf._graph_for_run(self.run_id)
        if graph is None:
            return ""
        done: dict[str, int] = {}
        for s in self.sf.get_steps(self.run_id):
            if s.get("status") == "completed":
                done[s["step_id"]] = done.get(s["step_id"], 0) + 1
        return pipeline_overview(
            graph, step_id, done,
            tools_delegated=bool(getattr(self.sf, "delegate_tools_to_agent", True)))

    def write_output_files(self, step_id: str, result: dict):
        """Write output_fixed files to step tmp_dir before confirm.

        Call this before ``action="submit"`` for steps that have
        ``output.mode="content"`` with ``output.fixed`` entries.
        SkillFlow's lifecycle hooks (step_commit) will promote the
        files from tmp_dir to step_dir during confirm_step.
        """
        if not self.sf._workspace or not self.run_id:
            return
        pid = self.sf._get_project_id(self.run_id)
        gname = self.sf._get_graph_name(self.run_id)
        try:
            # The graph THIS RUN is pinned to. Resolved by NAME the runner would
            # read a different graph than the engine handed it the step from —
            # and this one decides which files get STAGED (node.output_fixed),
            # so an edit landing mid-run made the engine validate one output
            # contract while the runner staged another.
            resolver = self.sf._get_resolver_for_run(self.run_id)
            node = resolver.get_node(step_id)
            if not node or not node.output_fixed:
                return
        except Exception:
            return
        claim = self._current_claim
        if claim is None or claim.step_id != step_id:
            raise RuntimeError("Output submission requires this step's active claim")
        for slot in node.output_fixed:
            if slot not in result:
                continue  # proxy-written slots must not be overwritten with empty text
            reply = self.sf.execute_tool("write_" + slot, {"content": result[slot]},
                run_id=self.run_id, step_id=step_id,
                step_instance_id=claim.token.step_instance_id, claim_epoch=claim.token.claim_epoch)
            if reply.get("error"):
                raise SlotRejected(f"Output {slot!r} rejected: {reply['error']}")

    # ── internal ──────────────────────────────────────────────────

    def _advance_and_respond(self) -> SkillResponse:
        """Advance the graph and return a SkillResponse for the agent."""
        # If a step is already claimed (waiting for submit), re-return it
        if self._current_claim is not None:
            return self._make_response(self._current_claim)

        loop_guard = 0
        while loop_guard < 100:
            loop_guard += 1
            self.sf.advance_run(self.run_id)

            run = self.sf.get_run(self.run_id)
            if run is None:
                return SkillResponse(status="failed", error="Run not found", run_id=self.run_id or "")

            status = run.get("status")

            if status == "completed":
                return self._completed_response(run)
            if status == "failed":
                return SkillResponse(status="failed",
                                     error=run.get("error_reason", "Unknown error"),
                                     run_id=self.run_id or "")
            if status == "paused":
                return self._paused_response(run)

            # Running — claim the next agent step
            claimed = self.sf.claim_next_step(self.run_id)
            if claimed is None:
                # Gate/tool auto-resolved, or no pending step — retry
                continue

            self._current_claim = claimed
            return self._make_response(claimed)

        return SkillResponse(status="failed",
                             error="Advance loop exceeded 100 iterations",
                             run_id=self.run_id or "")

    def _completed_response(self, run: dict) -> SkillResponse:
        """Collect final outputs from all completed steps."""
        # include_payloads: this is the one caller that reads outputs_json.
        steps = self.sf.get_steps(self.run_id, include_payloads=True)
        completed = [s for s in steps if s.get("status") == "completed"]
        outputs: dict = {}
        for s in completed:
            import json
            try:
                out = s.get("outputs_json", "{}")
                if isinstance(out, str):
                    out = json.loads(out) if out else {}
                outputs[s["step_id"]] = out
            except (json.JSONDecodeError, TypeError):
                outputs[s["step_id"]] = {}

        return SkillResponse(
            status="completed",
            run_id=self.run_id or "",
            outputs=outputs,
            steps_completed=len(completed),
        )

    def _paused_response(self, run: dict) -> SkillResponse:
        """Render retained completed data, never mutable checkpoint files."""
        try:
            checkpoint = self.sf.get_checkpoint(self.run_id)
        except SkillFlowError:
            # A host's ordinary manual pause is not a checkpoint approval.
            return SkillResponse(status="paused", run_id=self.run_id or "",
                                 checkpoint_label="Review")
        label = checkpoint["label"] or "Review"
        return SkillResponse(
            status="paused", run_id=self.run_id or "", step=checkpoint["step_id"],
            checkpoint_label=label, checkpoint_reject_to=checkpoint["reject_to"],
            expected_checkpoint=checkpoint["expected_checkpoint"], outputs=checkpoint["outputs"],
            instruction=f"Pipeline paused at checkpoint: {label}\nApprove to continue, or reject with feedback to request changes.",
        )
