# Stepflow

Config-agnostic LLM pipeline graph executor. Define your pipeline as a YAML DAG — stepflow handles traversal, tool execution, checkpoints, and recovery.

## Install

```bash
pip install -e ~/stepflow
```

## Quick Start

```python
from stepflow import StepFlow, PipelineGraph

# Load graph config (host app provides this)
graph = PipelineGraph.from_yaml("configs/my_pipeline.yaml")

sf = StepFlow(":memory:")
sf.register_graph(graph)

# Create and run
rid = sf.create_run("my_pipeline", context={"project_id": "demo"})
sf.start_run(rid)

# Poll loop
while True:
    next_node = sf.advance_run(rid)
    if next_node is None:
        break  # run complete or paused
    claimed = sf.claim_next_step(rid)
    if claimed is None:
        continue  # tool node auto-executed, advance again
    # Host app executes the agent step via StepRunner protocol
    result = await runner.execute(claimed)
    sf.confirm_step(claimed.token, result)
```

## Package

```
src/stepflow/
├── graph.py          # Data model: Transition, StepNode, PipelineGraph, GraphResolver
├── core.py           # StepFlow orchestrator: create/claim/confirm/advance/recover
├── tool_loader.py    # Dynamic tool schema + implementation import
├── context.py        # ContextResolver: cross-config, step-output, tool sources
├── step_validation.py # StepValidator: multi-tool validation
├── write_tools.py    # Constrained write tool generation from output.fixed
├── workspace.py      # Configurable workspace manager
├── validation.py     # Optional Pydantic output validation
├── recovery.py       # Stale claim recovery
├── outbox.py         # Outbox event consumer
├── schema.py         # SQLite DDL
├── exceptions.py     # StepFlowError hierarchy
└── tools/            # Native tools (10)
    ├── read_file/    # File reader with line numbers
    ├── write/        # File writer
    ├── list_tree/    # Directory lister
    ├── dir_tree/     # Context tree generator
    ├── web_search/   # SearXNG web search
    ├── web_fetch/    # URL fetcher with SSRF protection
    ├── json_schema/  # JSON Schema validator
    ├── syntax_lint/  # Syntax checker (ruff, compile)
    ├── py_compile/   # Python bytecode compile
    ├── pytest/       # Test runner
    ├── repo_apply/   # File → repo apply + git commit
    └── repo_validate/ # Multi-tool repo validation
```

## Graph Config Format

```yaml
name: "my_pipeline"
begin: "step_1"

steps:
  - id: "step_1"
    step_type: "agent"
    agent_config: "my_agent"
    context:
      - source: { config: "meta", output: "brief.md" }
      - source: { tool: "dir_tree" }
    output:
      mode: "content"
      fixed:
        result: "output.md"
    checkpoint: true
    checkpoint_label: "Review Output"
    transitions:
      - to: "step_1_review"
        match: { from: "checkpoint", value: "approved" }

  - id: "step_1_review"
    step_type: "agent"
    agent_config: "reviewer"
    transitions:
      - to: "step_2"
        match: { field: "passed", value: true }
      - to: "step_1"
        match: { field: "passed", value: false }
        max_loop: 3

  - id: "apply_to_repo"
    step_type: "tool"
    tool_name: "repo_apply"
    tool_params:
      source_dir: "$STEP_DRAFT_DIR"
    transitions:
      - to: "validate"
        match: { field: "applied", value: true }
      - to: "step_1"
        match: { field: "applied", value: false }
        max_loop: 3
        feedback: true
```

## Node Types

| Type | Description |
|------|-------------|
| `agent` | LLM agent step — host app executes via StepRunner |
| `gate` | Auto-resolved by stepflow using match conditions |
| `tool` | Auto-executed by stepflow via ToolLoader |

## Transition Match

```yaml
# Field match (read from step output JSON)
match: { field: "passed", value: true }

# Checkpoint routing
match: { from: "checkpoint", value: "approved" }

# Always match (default)
# (no match key)
```

## Feedback Loopback

```yaml
transitions:
  - to: "prev_step"
    match: { field: "all_passed", value: false }
    max_loop: 5
    feedback: true   # ← inject tool error into step input
```

## Custom Tools

Host apps add custom tool directories:

```python
from stepflow.tool_loader import ToolLoader

loader = ToolLoader(Path("stepflow/tools"))     # native
loader.add_tools_dir(Path("my_app/tools"))      # custom
```

Each tool directory contains `{name}/tool.yaml` + `{name}/impl.py`.

## Tests

```bash
pytest tests/ -v    # 228 tests, 89% coverage
```
