"""RunnerService — transport-neutral core for driving pipelines in runner mode.

One core, many transports:

- **In-process import** — a host agent loop (e.g. a butler/meta-agent) calls
  these functions directly and adds its own glue (sessions, UI, tool gating).
- **MCP** — ``mcp_server.py`` exposes the same functions as typed MCP tools
  for any MCP-speaking agent (Claude Code, opencode, ...).
- **CLI** — ``skillflow-run`` remains the lowest common denominator.

Every method is stateless per call: the run is reconnected from ``run_id`` and
all real state lives in SQLite + the workspace. A crashed client, a second
client, or an interleaved CLI call on the same run all behave identically.

The service also owns the protocol-level guards that every consumer needs:
one live run per (graph, project) — concurrent runs share step directories
and would corrupt promotion — and seed-file writing before start.
"""

from __future__ import annotations

from dataclasses import asdict

from skillflow.core import SkillFlow

from .runner import SkillTool


def _response_dict(resp) -> dict:
    """SkillResponse → plain dict, empty fields dropped."""
    d = asdict(resp)
    return {k: v for k, v in d.items() if v not in ("", {}, [], 0, None)}


class RunnerService:
    """Drive registered graphs in runner mode on behalf of any transport."""

    # Run states that block starting another run of the same graph+project.
    _LIVE_STATES = ("pending", "running", "paused")

    def __init__(self, sf: SkillFlow):
        self.sf = sf

    # ── helpers ────────────────────────────────────────────────────

    def _tool(self) -> SkillTool:
        return SkillTool(self.sf, "")  # graph resolved on reconnect via run_id

    def _reconnect(self, run_id: str) -> "tuple[SkillTool, dict | None]":
        """Reconnect a SkillTool to an existing run.

        Returns (tool, None) on success or (tool, error_dict). Reconnection
        resets stale claims and re-claims the in-flight step, so a client that
        died mid-step resumes cleanly.
        """
        if not run_id:
            return self._tool(), {"error": "run_id is required"}
        tool = self._tool()
        resp = tool(action="next", run_id=run_id)
        if resp.status == "failed" and resp.error.startswith("Run not found"):
            return tool, {"error": f"Run not found: {run_id}"}
        return tool, None

    # ── protocol surface ───────────────────────────────────────────

    def start(self, graph_name: str, *, project_id: str | None = None,
              seeds: dict | None = None) -> dict:
        """Start a new run: guard against concurrent runs, write seeds, claim.

        ``seeds`` is a {filename: content} map written into the graph's
        ``_seed`` config directory before the first step is claimed, so the
        graph's ``{config: <graph>, output: <filename>}`` context sources
        resolve on the very first instruction.
        """
        if graph_name not in getattr(self.sf, "_graphs", {}):
            known = sorted(getattr(self.sf, "_graphs", {}).keys())
            return {"error": f"Unknown graph '{graph_name}'. Registered: {known}"}

        if project_id:
            try:
                existing = self.sf.get_run_by_project(project_id, graph_name)
            except Exception:
                existing = None
            if existing and existing.get("status") in self._LIVE_STATES:
                return {"error": (
                    f"A '{graph_name}' run is already live for project "
                    f"'{project_id}' (run_id={existing['id']}, "
                    f"status={existing['status']}). Concurrent runs share step "
                    f"directories — resume it (runner_status / runner_submit) "
                    f"or finish it first.")}

        if seeds:
            if not project_id:
                return {"error": "seeds require a project_id (they live in the "
                                 "project's config workspace)"}
            if self.sf._workspace is None:
                return {"error": "No workspace configured — cannot write seeds"}
            seed_dir = (self.sf._workspace.get_config_path(project_id, graph_name)
                        / "_seed")
            seed_dir.mkdir(parents=True, exist_ok=True)
            for fname, content in seeds.items():
                if not isinstance(content, str):
                    import json as _json
                    content = _json.dumps(content, indent=2, ensure_ascii=False)
                (seed_dir / fname).write_text(content, encoding="utf-8")

        tool = SkillTool(self.sf, graph_name, project_id=project_id)
        return _response_dict(tool(action="next"))

    def next(self, run_id: str) -> dict:
        """Reconnect and return the current instruction (or paused/completed)."""
        tool, err = self._reconnect(run_id)
        if err:
            return err
        return _response_dict(tool(action="next", run_id=run_id))

    def status(self, run_id: str) -> dict:
        """Read-only run status — no claim reset, safe to poll."""
        run = self.sf.get_run(run_id)
        if run is None:
            return {"error": f"Run not found: {run_id}"}
        steps = self.sf.get_steps(run_id)
        return {
            "run_id": run_id,
            "graph": run.get("graph_name", ""),
            "project_id": run.get("project_id", ""),
            "status": run.get("status", ""),
            "current_node": run.get("current_node"),
            "steps_completed": [s["step_id"] for s in steps
                                if s.get("status") == "completed"],
        }

    def submit(self, run_id: str, step_id: str, result: dict | None = None) -> dict:
        """Confirm the current step and advance.

        ``result`` carries one key per output slot for content-mode steps
        (e.g. ``{"plan": "..."}``); pass ``{}`` when outputs were already
        staged via write tools (execute_step_tool). The result dict doubles
        as gate flags for transitions.
        """
        if not step_id:
            return {"error": "step_id is required"}
        result = result if isinstance(result, dict) else {}
        tool, err = self._reconnect(run_id)
        if err:
            return err
        current = tool(action="next", run_id=run_id)
        if current.status != "in_progress":
            return _response_dict(current)
        if current.step != step_id:
            return {"error": (f"Current step is '{current.step}', not "
                              f"'{step_id}' — do the work in its instruction "
                              f"first."),
                    "current": _response_dict(current)}
        if result:
            tool.write_output_files(step_id, result)
        return _response_dict(tool(action="submit", step_id=step_id,
                                   result=result))

    def approve(self, run_id: str) -> dict:
        """Approve a paused checkpoint. The checkpoint belongs to the human
        user — transports must only call this after explicit user approval."""
        tool, err = self._reconnect(run_id)
        if err:
            return err
        return _response_dict(tool(action="approve", run_id=run_id))

    def reject(self, run_id: str, feedback: str, redirect_to: str = "") -> dict:
        """Reject a paused checkpoint with feedback (optionally redirecting
        the run to an earlier step). Feedback reaches the re-claimed step."""
        if not (feedback or "").strip():
            return {"error": "feedback is required for reject"}
        tool, err = self._reconnect(run_id)
        if err:
            return err
        return _response_dict(tool(action="reject", run_id=run_id,
                                   feedback=feedback,
                                   redirect_to=redirect_to))

    def execute_step_tool(self, run_id: str, step_id: str, name: str,
                          params: dict | None = None,
                          project_root: str = "") -> dict:
        """Execute one of the step's skillflow tools (write/read/native).

        Proxies to ``sf.execute_tool`` — allowlist enforcement, staging-dir
        routing and durable tracing included. Host-side tools are bounced
        with a redirecting error instead of a confusing "not allowed": the
        boundary self-describes in both directions.
        """
        if not name:
            return {"error": "tool name is required"}
        run = self.sf.get_run(run_id)
        if run is None:
            return {"error": f"Run not found: {run_id}"}

        # Name-prefix guessing would misclassify common host tools
        # (edit_file, read_code_file...) as skillflow's — instead, check the
        # exact set of names this run's GRAPH can generate, plus registered
        # native tools. Anything outside that set is a host tool: redirect it
        # instead of returning a confusing "not allowed".
        known = (name in self._graph_tool_names(run.get("graph_name", ""))
                 or self._loader_has(name))
        if not known:
            return {"error": (
                f"'{name}' is not a skillflow tool — if it is one of your "
                f"host tools, call it directly, not through skillflow. Use "
                f"this proxy only for the tools named in the step "
                f"instruction (write_*/read_*/native tools).")}

        # step_instance_id: correlate the call to the claimed instance when
        # one exists (loop steps re-run the same step_id many times).
        instance_id = None
        try:
            for s in self.sf.get_steps(run_id):
                if s.get("step_id") == step_id and s.get("status") == "claimed":
                    instance_id = s.get("id")
                    break
        except Exception:
            pass

        return self.sf.execute_tool(
            name, params or {},
            run_id=run_id, step_id=step_id,
            step_instance_id=instance_id,
            project_root=project_root,
        )

    def _graph_tool_names(self, graph_name: str) -> set:
        """Every tool name this graph can generate (write/create/edit slots,
        finish_step, context read tools) across all its nodes."""
        names: set = set()
        graph = getattr(self.sf, "_graphs", {}).get(graph_name)
        if graph is None:
            return names
        from skillflow.write_tools import generate_write_tool_schemas
        try:
            from skillflow.read_tools import get_read_tool_names
        except ImportError:
            get_read_tool_names = None
        for node in getattr(graph, "steps", []):
            if getattr(node, "output_mode", ""):
                for ws in generate_write_tool_schemas(
                        node.output_mode, node.output_fixed,
                        allow_full_write=getattr(node, "output_allow_full_write",
                                                 False)):
                    names.add(ws["name"])
            if get_read_tool_names and getattr(node, "context", None):
                try:
                    names.update(get_read_tool_names(node.context))
                except Exception:
                    pass
        return names

    def _loader_has(self, name: str) -> bool:
        loader = getattr(self.sf, "_tool_loader", None)
        if loader is None:
            return False
        try:
            return name in loader.list_tools() or loader.is_dynamic(name)
        except Exception:
            return False
