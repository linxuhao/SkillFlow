# CLAUDE.md — Skillflow

Skillflow is a config-agnostic LLM pipeline graph executor. It is a pure Python library with minimal dependencies (PyYAML, ruff). Published on PyPI as `skillflow-py` (currently 1.1.3).
Under development, no backward compatibility is needed.

## Project Rules

- **Zero AItelier imports.** Skillflow must never import from `core/`, `api/`, `cli/`, `aitelier/`, `models/`, or `templates/`.
- **All tools are in `src/skillflow/tools/{name}/`** with `tool.yaml` + `impl.py`. Function name must match directory name.
- **Tests in `tests/`** (core) + `src/skillflow/plugins/*/tests/` (plugins). Run: `pytest tests/ src/skillflow/plugins/`
- **Backward compat:** New fields on StepNode/Transition must have defaults. Old YAML without new fields must still parse.
- **`type` field in tool.yaml is forbidden** — tools are callable by both agent steps and tool steps. Access control is via `agent_config.tools: [...]` (for agents) and `tool_name: "..."` (for tool nodes).

## Build & Test

Project uses `.venv/` at repo root (auto-detected by VS Code). First-time setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ~/skillflow[dev]
```

Then:

```bash
pytest tests/ -v                    # core tests
pytest src/skillflow/plugins/ -v    # plugin tests (linter, skill_runner, skill_converter)
```

## Architecture

```
PipelineGraph (YAML) → GraphResolver (validation + traversal)
                         ↓
SkillFlow (orchestrator)  ← SQLite (WAL mode)
  ├── claim_next_step()  → ClaimedStep → StepRunner (host app)
  ├── confirm_step()     → completed/failed
  ├── advance_run()      → resolve gates, auto-execute tools
  │   ├── recover_stale_claims() (built-in)
  │   └── feedback loopback (inject tool error into step inputs)
  ├── reject_checkpoint() → reset to pending
  └── drain_outbox()     → event stream

ToolLoader (multi-source)
  ├── Native: src/skillflow/tools/
  └── Custom: host app adds via add_tools_dir()

ContextResolver → assemble prompt context from:
  ├── {config, output}  (cross-config)
  ├── {step, output, mode}  (same-config)
  └── {tool}  (dynamic call)

StepValidator → run validation specs: [{files, tool, inline_schema}]
WriteTools → generate constrained write_* tools from output.fixed
```

## Key Data Structures

- `Transition(to, match, max_loop, label, feedback)` — directed edge
- `StepNode(id, step_type, transitions, checkpoint, config, tool_name, tool_params, agent_config, context, output_mode, output_fixed, validation)` — graph node
- `ClaimToken(step_id, run_id, step_instance_id, version, claimed_at)` — frozen claim
- `ClaimedStep(token, step_config, run_context, inputs, validation_error)` — ready to execute
- `StepResult(outputs, flags)` — execution result (flags used for transition matching)
- `StepRunner` — Protocol: `async def execute(step: ClaimedStep) -> StepResult`

## Tools

Each tool: `tool.yaml` (schema) + `impl.py` (function). Function signature:

```python
def tool_name(*params, *, workspace_root: str = "", project_root: str = "") -> dict:
    ...
    return {"verdict": "passed"} | {"verdict": "failed", "feedback": "..."}
```

Native tools (13): read_file, write, list_tree, dir_tree, json_schema, syntax_lint, py_compile, pytest, repo_apply, repo_validate, draft_commit, file_exists, notify.

## CLIs (console scripts)

- `skillflow-run` — **runner mode**: an external agent drives a pipeline step-by-step over a stateless CLI (host-delegated; agents declared `model: "host"`). Bring-your-own-agent — any CLI agent (Claude CLI, Codex, …) can drive a pipeline and inherit the trust properties.
- `skillflow-convert` — turn a plain-language skill/workflow description into a runnable SkillFlow pipeline YAML.
- `skillflow-lint` — validate a pipeline YAML (graph cycle-safety, reachability, schema).

## Plugins (`src/skillflow/plugins/`)

- `linter` — pipeline-YAML linter; also exposed as the `skillflow_lint` tool + the `skillflow-lint` CLI.
- `skill_runner` — host-delegated runner mode. Three transports over one core: `RunnerService` (`service.py` — in-process embed: start guard, seeding, stateless reconnect, `execute_step_tool` proxy with host-tool redirect, finish_step→submit mapping), the `skillflow-run` CLI, and `skillflow-mcp` (`mcp_server.py`, typed MCP tools over stdio; optional extra `skillflow-py[mcp]`). PromptAssembler emits a transport-neutral slot contract — never advertise write_*/finish_step as directly-callable functions.
- `skill_converter` — skill→pipeline graph (host-mode agents) behind `skillflow-convert`; AItelier registers it to power its `generate_pipeline` butler tool.

## Host integration

- `WorkspaceManager(code_path_resolver=…)` — optional hook mapping a project to a host-managed code path (e.g. an existing repo), so `repo_apply` commits into the real repo. `SkillFlow(code_path_resolver=…)` forwards it.
