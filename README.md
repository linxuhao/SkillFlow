# Stepflow

Config-agnostic LLM pipeline graph executor. Define multi-agent pipelines as YAML DAGs — stepflow handles traversal, tool execution, checkpoints, recovery, and event streaming on SQLite.

## Install

```bash
pip install stepflow          # PyPI
pip install -e ~/stepflow     # from repo (editable)
```

Or clone and use the install script, which also registers CLI commands:

```bash
git clone https://github.com/your-org/stepflow.git
bash stepflow/scripts/install.sh
```

CLI commands registered in `~/.local/bin/`:

| Command | Description |
|---------|-------------|
| `stepflow-lint` | Validate pipeline YAML files |
| `stepflow-run` | Interactive pipeline runner (human-in-the-loop) |

### PyPI publish

```bash
pip install build twine
python3 -m build
twine upload dist/*
```

## Getting Started

Stepflow runs pipelines in two modes.

### Framework Mode

Stepflow is embedded in a host application. The host drives the loop — stepflow handles traversal, tool execution, and state. The host only executes agent steps via `StepRunner`.

```python
from stepflow import StepFlow, PipelineGraph, StepResult

graph = PipelineGraph.from_yaml("tests/fixtures/minimal_1step.yaml")

sf = StepFlow(":memory:")
sf.register_graph(graph)
sf.register_agent_config("echo_agent", model="host")

run_id = sf.create_run("minimal_1step")
sf.start_run(run_id)

while True:
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    if claimed is None:
        break  # completed or paused
    # Host StepRunner executes the agent step here
    sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))
```

In framework mode, **all tools auto-execute** inline — stepflow runs native tools and custom tools without involving the host agent.

Config reference: `tests/fixtures/minimal_1step.yaml`.

### Runner Mode

Stepflow is driven interactively via `SkillTool`. The pipeline exposes steps as instructions — a human or LLM agent calls `action="next"` / `"submit"` / `"approve"` / `"reject"`.

```python
from stepflow import StepFlow, PipelineGraph
from plugins.skill_runner import SkillTool

graph = PipelineGraph.from_yaml("tests/fixtures/skill_review.yaml")
sf = StepFlow(":memory:", delegate_tools_to_agent=True)
sf.register_graph(graph)
sf.register_agent_config("review_analyst", model="host")
# ... register other agent configs referenced by the graph

tool = SkillTool(sf, "skill_review")
resp = tool(action="next")

while resp.status == "in_progress":
    print(resp.instruction)
    # Agent does work, produces output...
    resp = tool(action="submit", result={"findings": {...}})
# resp.status == "completed"
```

In runner mode, **native tools auto-execute** but **custom and unknown tools are delegated** to the agent (via `resp.tool_name` / `resp.tool_params`).

Use the CLI for interactive human-in-the-loop runs:

```bash
stepflow-run tests/fixtures/skill_review.yaml
```

## Node Types

| Type | Description |
|------|-------------|
| `agent` | LLM step — host app executes via `StepRunner` protocol |
| `tool` | Auto-executed by stepflow (native), or delegated to agent in runner mode (custom) |
| `gate` | Auto-resolved using match conditions against step output flags |
| `loop` | Iterates over a JSON list from a workspace file, instantiating sub-steps per item |

## Transition Matching

Five match strategies. See `tests/fixtures/dpe_full.yaml` for a complete pipeline using all of them:

```yaml
match: { field: "passed", value: true }                          # step output flags
match: { from_file: "review_verdict.json", field: "passed", value: true }  # output file
match: { from: "checkpoint", value: "approved" }                 # checkpoint routing
match: { _error: true }                                          # error handler
# (no match key)                                                 # always match
```

## Context Injection

```yaml
context:
  - source: { step: "1" }
  - source: { step: "2", mode: "interfaces" }
  - source: { config: "meta", output: "brief.md" }
  - source: { tool: "dir_tree" }
```

## Checkpoints

Agent steps can pause for human approval (`tests/fixtures/checkpoint_cycle.yaml`):

```python
sf.reject_checkpoint(run_id, "draft", "Add more detail to the analysis")
```

## Output Validation

Steps declare validation specs auto-executed by stepflow. See `tests/fixtures/skill_review.yaml` for inline JSON Schema validation, or `tests/fixtures/lifecycle_hooks.yaml` for syntax_lint + py_compile validators.

Available validators: `json_schema`, `syntax_lint`, `py_compile`, `pytest`, `file_exists`.

## Lifecycle Hooks

Steps with `output.mode: "write"` can trigger deliver and post-deliver hooks. See `tests/fixtures/lifecycle_hooks.yaml`:

```yaml
lifecycle:
  on_deliver:
    tool: "repo_apply"
    params:
      source_dir: "$STEP_DIR"
    on_failure: "retry"
    max_retries: 2
  after_deliver:
    - tool: "syntax_lint"
      files: ["*.py"]
```

## Error Handling

Steps declare `max_retries` and an `_error` transition. See `tests/fixtures/error_handler.yaml`.

## Feedback Loopback

Tool failures can inject output into the next step's inputs (`feedback: true`). See `plugins/skill_converter/skill_converter.yaml` — the `validate_design` step feeds lint errors into `fix_issues`.

## End Conditions

Four termination strategies, combined with `and`/`or`. See `tests/fixtures/end_conditions.yaml` and `tests/fixtures/dpe_full.yaml`:

```yaml
end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: "5_review"
      result: "completed"
    - type: max_total_steps
      limit: 200
    - type: max_run_duration_seconds
      limit: 3600
    - type: flag_match
      flag: { fatal_error: true }
```

## Stale Claim Recovery

Built into `advance_run`. Claims older than `stale_threshold_seconds` (default 300) are auto-reset:

```python
sf = StepFlow("pipeline.db", stale_threshold_seconds=300)
```

## Event Streaming

All state transitions are written to `stepflow_outbox`. Poll for real-time notifications:

```python
events = sf.drain_outbox(batch_size=50)
for event in events:
    print(event.event_type, event.payload)
sf.ack_outbox([e.id for e in events])
```

In-process subscribers via `NotificationBus`:

```python
from stepflow import NotificationBus

bus = NotificationBus()
bus.subscribe("step_completed", lambda n: print(n.payload))
sf = StepFlow(":memory:", notification_bus=bus)
```

## Tools

### Native (13 built-in)

| Tool | Description |
|------|-------------|
| `read_file` | Read a file with line numbers |
| `write` | Write content to workspace |
| `list_tree` | List directory structure |
| `dir_tree` | Context tree for prompt injection |
| `json_schema` | Validate JSON against inline schema |
| `syntax_lint` | Syntax check via ruff |
| `py_compile` | Python bytecode compile |
| `pytest` | Run pytest on test files |
| `repo_apply` | Copy files to repo + git commit |
| `repo_validate` | Multi-tool repo validation |
| `draft_commit` | Move draft files to final dir + commit |
| `file_exists` | Check files matching glob patterns |
| `notify` | Send user-visible notifications |

### Custom tools

Host apps add tool directories. Each tool: `{name}/tool.yaml` + `{name}/impl.py`. Function name must match directory name.

```python
from stepflow.tool_loader import ToolLoader

loader = ToolLoader()
loader.add_tools_dir("my_app/tools")
sf = StepFlow(":memory:", tool_loader=loader)
```

## Plugins

### Linter (`plugins/linter/`)

Validates stepflow pipeline YAML configs. Usable as a stepflow tool (`stepflow_lint`) or standalone:

```bash
stepflow-lint tests/fixtures/skill_review.yaml
stepflow-lint configs/*.yaml
```

### Skill Runner (`plugins/skill_runner/`)

Stateful callable wrapping a pipeline as an agent tool. Supports `next`, `submit`, `approve`, `reject` actions. Returns `SkillResponse` with instruction, available tools, and step metadata.

### Skill Converter (`plugins/skill_converter/`)

Meta-pipeline that converts a skill description into a stepflow YAML config. See `plugins/skill_converter/skill_converter.yaml`.

## Package

```
src/stepflow/
├── core.py              # StepFlow orchestrator (create/claim/confirm/advance)
├── graph.py             # PipelineGraph, StepNode, Transition, GraphResolver
├── tool_loader.py       # Dynamic tool schema + implementation loading
├── context.py           # ContextResolver: cross-config, step, tool sources
├── step_validation.py   # StepValidator: multi-tool output validation
├── write_tools.py       # Constrained write tool generation from output.fixed
├── workspace.py         # Per-step atomic staging directories
├── validation.py        # Optional external-schema output validation
├── recovery.py          # Stale claim recovery
├── schema.py            # SQLite DDL + migrations
├── exceptions.py        # StepFlowError hierarchy
├── outbox.py            # OutboxConsumer for event polling
├── notifications.py     # NotificationBus for in-process subscribers
├── agent_registry.py    # Agent config registry + schema resolution
└── tools/               # Native tools (13)
    ├── read_file/       ├── write/          ├── list_tree/
    ├── dir_tree/        ├── json_schema/    ├── syntax_lint/
    ├── py_compile/      ├── pytest/         ├── repo_apply/
    ├── repo_validate/   ├── draft_commit/   ├── file_exists/
    └── notify/
```

## Tests

```bash
pytest tests/ -v       # 306 tests
pytest plugins/ -v     # 21 plugin tests
```
