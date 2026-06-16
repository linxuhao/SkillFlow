# Skillflow

[![PyPI](https://img.shields.io/pypi/v/skillflow-py)](https://pypi.org/project/skillflow-py/)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB)
![License](https://img.shields.io/badge/license-MIT-blue)

Config-agnostic LLM pipeline graph executor with **human-in-the-loop by design**. Define multi-agent pipelines as YAML DAGs — skillflow handles deterministic traversal, tool execution, approval/reject checkpoints, loops, recovery, a full durable audit trace, and event streaming on SQLite. Provider-agnostic, custom tools, clean per-agent context.

Two ways agents plug in: **embed** skillflow in a host app so it *drives* real agents step-by-step (forced execution order — see [Framework Mode](#framework-mode)), or have an **external agent** (e.g. Goose) drive pipelines over a stateless CLI ([Runner Mode](#runner-mode)). An agent can even *generate* a pipeline from a natural-language description (`skillflow-convert`) and then execute it.

## Why Skillflow

Most agent frameworks let the LLM improvise control flow, tool use, and file access — which is exactly why their runs can't be reproduced, audited, or trusted. Skillflow inverts that: **the LLM is a constrained, contract-bound function; the engine is the runtime.**

- **Deterministic traversal** — the pipeline is a YAML DAG walked by the engine. Loops, gates, retries, and recovery are the engine's job, not the model's. Same config, same path.
- **Capability-gated I/O (least privilege)** — a step sees only the context it declares, and for each declared output the engine *generates a dedicated write tool* (`write_<slot>` / `create_<slot>` / `append_<slot>`). An agent literally **cannot read or write a file outside its contract** — the tool to do so doesn't exist in its schema. *Brain to brain, tools to tools.* (It's also why cheap models suffice: small, focused, role-scoped context.)
- **Human-in-the-loop by design** — approve / reject-with-feedback checkpoints are first-class nodes, not bolted on.
- **Immutable audit trace** — every step, prompt, response, tool call, and verdict is appended to a trace that is *never deleted*, keyed by `step_instance_id`. "Why did this run do X?" is one query.
- **Config- and provider-agnostic** — a pipeline can be anything; nothing is hardcoded to a use case or a model.

## Install

```bash
pip install skillflow-py      # PyPI
pip install -e ~/skillflow    # from repo (editable)
```

`pip install` gives you the **library** (framework mode — `from skillflow import SkillFlow`). The **`skillflow-*` CLI commands are not pip entry points**; register them with the install script (it writes wrappers into `~/.local/bin/`):

```bash
git clone https://github.com/linxuhao/SkillFlow.git
bash skillflow/scripts/install.sh
```

CLI commands registered in `~/.local/bin/`:

| Command | Description |
|---------|-------------|
| `skillflow-lint` | Validate pipeline YAML files (one-shot) |
| `skillflow-run` | Stateless pipeline runner (agent calls via CLI) |
| `skillflow-convert` | Convert a skill description → pipeline YAML |

```bash
skillflow-lint configs/*.yaml                       # one-shot validation
skillflow-run --graph pipeline.yaml --action start  # start a pipeline (returns JSON)
skillflow-run --action submit --run-id <id> --result '{"key": "val"}'
skillflow-convert --desc "Code review skill..." --action start  # start from inline text
skillflow-convert --desc-file my_skill.md --action start        # or from a file
```

### PyPI publish

```bash
pip install build twine
python3 -m build
twine upload dist/*
```

## Two modes

Skillflow has two distinct modes — one for embedding in code, one for LLM agents.

| | Framework mode | Runner mode |
|---|---|---|
| **Interface** | Python library (`from skillflow import SkillFlow`) | CLI tools (`skillflow-run`, `skillflow-convert`) |
| **State** | In-process (or shared SQLite) | Stateless — each CLI call is a fresh process, state in SQLite |
| **Tool execution** | All tools auto-execute inline | Native tools auto-execute, everything else delegated to the agent |
| **delegate_tools_to_agent** | `False` (default) | `True` (hardcoded) |
| **Use case** | Embed skillflow in a host app | LLM agent drives pipelines via shell commands |

### Framework Mode

Skillflow is embedded in a host application. The host drives the loop — skillflow handles traversal, tool execution, and state. The host only executes agent steps via `StepRunner`.

```python
from skillflow import SkillFlow, PipelineGraph, StepResult

graph = PipelineGraph.from_yaml("tests/fixtures/minimal_1step.yaml")

sf = SkillFlow(":memory:")
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

Config reference: `tests/fixtures/minimal_1step.yaml`.

### Runner Mode

Runner mode is the **language-agnostic** interface for LLM agents. Agents drive pipelines by calling CLI tools — `skillflow-run` and `skillflow-convert`. Each invocation is a **fresh process** that reads state from SQLite, does one thing, prints JSON, and exits. The agent loops: call → parse JSON → act → call again.

Pass `--graph` **once** with `--action start`. The graph path is stored in the DB. All subsequent calls use `--run-id` to reconnect — no `--graph` needed.

```bash
# 1. Start a pipeline — pass --graph once, get the first step back
$ skillflow-run --graph pipeline.yaml --action start
{"status": "in_progress", "run_id": "abc123", "step": "analyze", "instruction": "..."}

# 2. Submit work for the current step (no --graph needed)
$ skillflow-run --action submit --run-id abc123 \
    --result '{"issues": [{"file": "app.py", "severity": "high"}]}'
{"status": "in_progress", "run_id": "abc123", "step": "summarize", "instruction": "..."}

# 3. When a checkpoint step completes, the run pauses
{"status": "paused", "checkpoint_label": "Review Summary — approve to commit, reject to revise"}

# 3a. Human approves (no --graph needed)
$ skillflow-run --action approve --run-id abc123
{"status": "in_progress", "run_id": "abc123", "step": "apply_fixes", ...}

# 3b. Or human rejects with feedback
$ skillflow-run --action reject --run-id abc123 \
    --feedback "Severity of bare except should be high, not medium"

# 4. Loop continues until the pipeline completes
{"status": "completed", "steps_completed": 3, "outputs": {...}}
```

**The agent drives this inline — there is no driver code.** The agent itself calls `skillflow-run` as a command inside its own turn loop: call → read the JSON → do the work (stage the expected output files, or run a delegated tool) → call again, reacting to `status` each turn (`in_progress` → submit, `paused` → ask the human to approve/reject, `completed`/`failed` → done). *The agent is the loop* — it never writes a program to drive skillflow. The full, injectable manual is [`AGENT.md`](src/skillflow/plugins/skill_runner/AGENT.md); load it via `load_agent_guide()` from `skillflow.plugins.skill_runner` and put it in the agent's system prompt.

**Response fields beyond status:**

| Field | When present | Meaning |
|-------|-------------|---------|
| `output_dir` | Steps with `output.fixed` | `.tmp` staging dir — write expected files here; skillflow promotes them on submit |
| `expected_files` | Steps with `output.fixed` | File names to create (e.g. `["findings.json"]`) |
| `validation_error` | Submit rejected by validator | Why the previous submit failed — fix and re-submit |
| `tool_name` | Tool steps | Tool the agent must execute |
| `tool_params` | Tool steps | Parameters for the tool |
| `tools` | Agent steps | Write helpers (`write_*`, `create_*`, `append_*`) with format specs |

## Node Types

| Type | Description |
|------|-------------|
| `agent` | LLM step — host app executes via `StepRunner` protocol |
| `tool` | Auto-executed by skillflow (native), or delegated to agent in runner mode (custom) |
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

## Loopback (review & goal loops)

Any transition's `to` can point **backward** to an earlier step — that's how review and goal loops are built. `max_loop` caps how many times an edge may fire per run (tracked in `skillflow_edge_counts`); once the cap is hit the edge stops matching, so the run takes another branch instead of looping forever. Set `feedback: true` to inject the step's outputs as `_feedback` into the target on the way back, making the redo corrective.

```yaml
# A Red-checker that sends work back to the maker until it passes (max 3×)
transitions:
  - to: "implement"          # backward edge → redo the step
    match: { passed: false }
    max_loop: 3
    feedback: true           # pass review notes back into 'implement'
  - to: "next_step"          # forward once the checker passes
    match: { passed: true }
```

This powers both **inner review loops** (e.g. `review → implement`, `max_loop: 3`) and **goal loops** (a final verifier routing back to planning until goals are met). See `tests/fixtures/review_loop.yaml` and `tests/fixtures/dpe_full.yaml`.

## Context Injection

```yaml
context:
  - source: { step: "1" }
  - source: { step: "2", mode: "interfaces" }
  - source: { config: "meta", output: "brief.md" }
  - source: { tool: "dir_tree" }
```

## Checkpoints

Agent steps can pause for human approval (`tests/fixtures/checkpoint_cycle.yaml`). On reject, the feedback is injected (via the `_feedback` channel) so the re-run knows *why* it was rejected:

```python
# Redo the rejected step itself
sf.reject_checkpoint(run_id, "draft", "Add more detail to the analysis")

# Or loop back to a DIFFERENT step — reopen the run earlier and carry the
# feedback to that target (e.g. reject the final review back to planning)
sf.reject_checkpoint(run_id, "final_review", "Goals not met", redirect_to="plan")
```

`redirect_to` makes rejection a human-driven loopback: it sets the run's current node to the target step and injects the feedback there. Over the CLI this is `--redirect-to <step>`. A checkpoint can also be rejected *after* a downstream failure (the only invariant is that the checkpoint step is `completed`), so you can reopen earlier work to recover.

## Output Validation

Steps declare validation specs auto-executed by skillflow. See `tests/fixtures/skill_review.yaml` for inline JSON Schema validation, or `tests/fixtures/lifecycle_hooks.yaml` for syntax_lint + py_compile validators.

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

Built into `advance_run`. A claim whose worker died before calling `confirm`, and that is older than `stale_threshold_seconds` (default 300), is auto-reset to `pending` and re-claimed:

```python
sf = SkillFlow("pipeline.db", stale_threshold_seconds=300)
```

**Crash-loop guard:** if the *same* step instance is recovered 3 times, skillflow stops retrying and marks it `failed` (`"worker crashed 3 times — likely a code bug or OOM"`), emitting a non-retryable `step_failed` event — so a buggy or OOM-prone step can't loop forever.

## Event Streaming

All state transitions are written to `skillflow_outbox`. Poll for real-time notifications:

```python
events = sf.drain_outbox(batch_size=50)
for event in events:
    print(event.event_type, event.payload)
sf.ack_outbox([e.id for e in events])
```

In-process subscribers via `NotificationBus`:

```python
from skillflow import NotificationBus

bus = NotificationBus()
bus.subscribe("step_completed", lambda n: print(n.payload))
sf = SkillFlow(":memory:", notification_bus=bus)
```

## Durable Run Trace

Unlike the outbox (drained + ack'd for delivery), the **trace is an append-only audit log that is never deleted**. It records every step claim/completion, prompt, model response, tool call + result, and lifecycle-hook outcome — keyed by `step_instance_id`, so loop iterations never overwrite one another and a finished run can be reconstructed offline. Tool calls are traced across **all** invocation paths (agent-invoked incl. custom tools, tool-step nodes, lifecycle hooks, validators), each tagged with a `source`.

```python
sf.trace(run_id, "event", "note", {"x": 1})          # hosts can add their own records
records = sf.get_trace(run_id)                        # chronological, by seq
for r in records:
    print(r["seq"], r["category"], r["event"], r["payload"])
```

Within a host, the claimed step exposes `step.trace(category, event, payload)` so prompts/responses land in the same timeline. Long fields are clipped; writes are cheap (an in-process per-run seq counter avoids a SELECT per record). Retention is the host's call:

```python
SkillFlow(db, trace_enabled=False)                   # opt out entirely (zero overhead)
sf.prune_trace(run_id="...")                          # drop one run's trace
sf.prune_trace(keep_last_runs=200)                    # cap to recent runs
```

`delete_project` removes a project's trace automatically. This is what turns "why did this run do X?" from forensic git-archaeology into one query.

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
from skillflow.tool_loader import ToolLoader

loader = ToolLoader()
loader.add_tools_dir("my_app/tools")
sf = SkillFlow(":memory:", tool_loader=loader)
```

### Auto-injected: `finish_step`

Every agent step (both `content` mode and `write` mode) automatically gets a `finish_step` tool injected as the **last** tool in the schema. This gives the agent a deterministic way to signal "I'm done writing outputs" instead of relying on the host runner to guess when the agent has stopped calling tools.

**Contract:**

- Skillflow generates the tool schema (always last in the array — the model is shown it last so it calls it last).
- Skillflow's tool executor returns `{"status": "completed"}` — a no-op.
- The **host runner** detects `finish_step` in the tool-calling loop and breaks the turn loop, proceeding to output validation and step completion.
- The runner MUST process **all** tool calls in the current turn before checking for `finish_step`, so multi-tool responses (e.g. `write_sota` + `finish_step`) work correctly — never short-circuit mid-turn.

## Use Cases

### 1. Framework mode — embed skillflow in your app

Use skillflow as a library. Read the [Getting Started](#getting-started) section above and the fixture examples in `tests/fixtures/`.

```python
from skillflow import SkillFlow, PipelineGraph
graph = PipelineGraph.from_yaml("my_pipeline.yaml")
sf = SkillFlow(":memory:")
sf.register_graph(graph)
# ... drive the loop with claim_next_step / confirm_step
```

### 2. Agent mode — convert skills to pipelines

`skillflow-convert` is a thin wrapper that calls `skillflow-run` with the built-in converter pipeline. The agent drives it the same way:

```bash
# Start conversion with a skill description
$ skillflow-convert --desc "Code review skill..." --action start
{"status": "in_progress", "run_id": "abc123", "step": "analyze_skill", "instruction": "..."}

# Submit analysis, continue through design → explain → lint → done (no --desc needed)
$ skillflow-convert --action submit --run-id abc123 --result '{"analysis": {...}}'
```

On completion, the generated pipeline YAML is at `~/.skillflow/workspaces/skill-converter/.../skill_pipeline.yaml`.

Agent manuals (the tool schema + rules) are shipped in the package:

| Plugin | Manual | Load via |
|--------|--------|----------|
| `skill_runner` | Actions, response format, rules | `load_agent_guide()` from `skillflow.plugins.skill_runner` |
| `skill_converter` | Step-by-step: analyze → design → lint → fix | `load_agent_guide()` from `skillflow.plugins.skill_converter` |

Inject these into the agent's system prompt so it knows how to call the CLI tools.

```bash
skillflow-lint pipeline.yaml                             # one-shot config validation
skillflow-run --graph pipeline.yaml --action start        # start a pipeline (returns JSON, --graph only once)
skillflow-convert --desc "..." --action start             # start a conversion
```

### Linter (`skillflow.plugins.linter`)

Framework utility. Validates pipeline YAML — used as a skillflow tool (`skillflow_lint`) inside the converter's feedback loop, or standalone:

```bash
skillflow-lint tests/fixtures/skill_review.yaml
skillflow-lint configs/*.yaml
```

## Package

```
src/skillflow/
├── core.py              # SkillFlow orchestrator (create/claim/confirm/advance)
├── graph.py             # PipelineGraph, StepNode, Transition, GraphResolver
├── tool_loader.py       # Dynamic tool schema + implementation loading
├── context.py           # ContextResolver: cross-config, step, tool sources
├── step_validation.py   # StepValidator: multi-tool output validation
├── write_tools.py       # Constrained write tool generation from output.fixed
├── workspace.py         # Per-step atomic staging directories
├── validation.py        # Optional external-schema output validation
├── recovery.py          # Stale claim recovery
├── schema.py            # SQLite DDL + migrations
├── exceptions.py        # SkillFlowError hierarchy
├── outbox.py            # OutboxConsumer for event polling
├── notifications.py     # NotificationBus for in-process subscribers
├── agent_registry.py    # Agent config registry + schema resolution
├── plugins/             # Built-in plugins
│   ├── linter/          # Config validator + skillflow_lint tool
│   ├── skill_runner/    # SkillTool — interactive pipeline facade
│   └── skill_converter/ # Skill description → pipeline YAML
└── tools/               # Native tools (13)
    ├── read_file/       ├── write/          ├── list_tree/
    ├── dir_tree/        ├── json_schema/    ├── syntax_lint/
    ├── py_compile/      ├── pytest/         ├── repo_apply/
    ├── repo_validate/   ├── draft_commit/   ├── file_exists/
    └── notify/
```

## Tests

```bash
pytest tests/ -v                    # 330+ tests
pytest src/skillflow/plugins -v     # 27 plugin tests
```
