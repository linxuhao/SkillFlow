# Design SkillFlow Graph

You are a skillflow pipeline designer. Convert the skill analysis into a valid skillflow YAML configuration.

## Instructions

Based on the analysis from the previous step, design a complete skillflow pipeline graph.

### Step Types

- `agent` — executed by the host LLM, needs `agent_config` (a descriptive name for the role)
- `gate` — auto-resolved by skillflow based on result flags, no LLM execution
- `tool` — auto-executed inline (e.g., validation tools)
- `loop` — iterates over a list from a workspace file

### Transition matching

- Simple: `{to: "next_step"}` (default, always taken)
- Flag-based: `{to: "branch_a", match: {route: "a"}}` — matches when step result has `route: "a"`
- Error handler: `{to: "error_handler", match: {_error: true}}`
- Checkpoint: `{to: "next", match: {from: "checkpoint", value: "approved"}}`
- Review loop: `{to: "prev_step", match: {approved: false}, max_loop: 3}`

### End conditions (at least one required)

```yaml
end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: "final_step"
      result: "completed"
    - type: max_total_steps
      limit: 50
```

### Key rules

1. Every agent node needs `agent_config` with a role name
2. Every cycle must have `max_loop` on at least one edge
3. Terminal nodes need end_conditions
4. Checkpoint nodes need `checkpoint: true` and a checkpoint transition
5. Use `tool_name: "skillflow_lint"` for validation steps (NOT "yaml_valid")

## Output Format

Output a complete skillflow YAML file `skill_pipeline.yaml`.
