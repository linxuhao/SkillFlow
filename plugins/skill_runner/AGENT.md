# run_skill — Stepflow Pipeline Executor

Execute a stepflow pipeline by calling this tool interactively. You never see the graph — the tool tells you what to do next, you do the work, you submit.

## Tool Schema

```json
{
  "name": "run_skill",
  "description": "Execute a stepflow pipeline step by step. Call with action='next' to start, then submit results to advance.",
  "parameters": {
    "action": {
      "type": "string",
      "enum": ["next", "submit", "approve", "reject"]
    },
    "step_id": {
      "type": "string",
      "description": "The step id from the SkillResponse. Required for 'submit' and 'reject'."
    },
    "result": {
      "type": "object",
      "description": "Your output for this step. Keys double as transition match flags."
    },
    "feedback": {
      "type": "string",
      "description": "Required for 'reject' — why the checkpoint was rejected."
    }
  }
}
```

## Interaction Protocol

Only manual, interactive mode. The agent calls the tool, receives a response, does work, submits.

```
Agent                         run_skill
  │                                │
  │── action="next" ───────→      │  creates run, claims first step
  │←── SkillResponse ─────────    │  {status: "in_progress", step, instruction, tools}
  │                                │
  │  [do the work]                 │
  │                                │
  │── action="submit" ──────→     │  confirms step, advances graph
  │    step_id="...",              │  (auto-resolves gates, tools, loops)
  │    result={...}                │
  │←── SkillResponse ─────────    │  {status, step, instruction, tools}
  │                                │
  │  ... repeat ...                │
  │                                │
  │←── SkillResponse ─────────    │  {status: "completed", outputs: {...}}
```

## SkillResponse Format

### In progress (work to do)
```json
{
  "status": "in_progress",
  "step": "analyze_diff",
  "instruction": "## Context\n\n### Source\n...\n\n## Task\nExecute step `analyze_diff`.",
  "tools": {
    "read_file": {"name": "read_file", ...},
    "grep": {...}
  }
}
```
Call `submit` with your result to advance.

### Paused at checkpoint
```json
{
  "status": "paused",
  "step": "summarize",
  "checkpoint_label": "Review Summary",
  "instruction": "Pipeline paused. Call approve or reject."
}
```
Call `approve` to continue, or `reject` with feedback to redo the step.

### Completed
```json
{
  "status": "completed",
  "outputs": {
    "analyze_diff": {"findings": [...]},
    "summarize": {"review": "..."}
  },
  "steps_completed": 5
}
```
The pipeline is done. Present `outputs` to the user.

### Failed
```json
{
  "status": "failed",
  "error": "No matching transition from 'review' with flags {...}"
}
```
Report the error to the user.

## Rules

1. Start with `action="next"`
2. On `status="in_progress"`: do the work, then `action="submit"` with `step_id` and `result`
3. On `status="paused"`: decide — `action="approve"` or `action="reject"` with feedback
4. On `status="completed"`: done — present outputs
5. On `status="failed"`: report error
6. Never `submit` twice in a row — wait for a new `in_progress`
7. `action="next"` while a step is pending returns the same instruction (idempotent)

## Tool nodes

When StepFlow is configured with `delegate_tools_to_agent=True`, tool nodes
are NOT auto-executed. Instead they're presented to you as regular steps
with `tool_name` and `tool_params`:

```json
{
  "status": "in_progress",
  "step": "validate_design",
  "tool_name": "stepflow_lint",
  "tool_params": {"path": "/workspace/design/skill_pipeline.yaml"},
  "instruction": "Execute tool: stepflow_lint"
}
```

**You** execute the tool (using your own tool infrastructure), then submit
the result. The runner stores it and advances the graph.

Without delegation (default), tool nodes are auto-executed by stepflow
and you never see them.

## Checkpoints are for your user, not you

When the runner returns `{status: "paused"}`, **present the checkpoint to
the human user behind you**. Do NOT auto-approve or reject.

```json
{
  "status": "paused",
  "step": "summarize",
  "checkpoint_label": "Review Summary — approve to commit, reject to revise",
  "instruction": "Pipeline paused at checkpoint. Call approve or reject."
}
```

Your job:
1. Show the checkpoint label and outputs to the user
2. Ask if they approve
3. If yes → `action="approve"`
4. If no → `action="reject"` with the user's feedback

## What you don't need to worry about

- **Gates** — auto-resolved, never shown to you
- **Tool nodes** (without delegation) — auto-executed inline, never shown
- **Loop steps** — auto-iterated, each iteration appears as a regular agent step
- **Error handlers** — routed automatically on retry exhaustion
- **Stale claims** — auto-recovered by advance_run
