# skill_converter — Skill-to-Pipeline Converter

Convert a skill description (markdown) into a valid stepflow pipeline YAML config. The converter is a stepflow pipeline — you interact with it through `run_skill`, same as any other skill.

## How to use

```python
from plugins.skill_converter import setup_converter

# From a string:
tool = setup_converter(sf, description="# Code Review Skill\n...")

# Or from a file (easier for agents to edit):
tool = setup_converter(sf, description_file="skill_description.md")

# tool is a SkillTool pointing at the "skill_converter" graph
```

Then drive it interactively with `run_skill`:

```
run_skill(action="next")
  → {status: "in_progress", step: "analyze_skill", instruction: "You are a pipeline architect...", tools: {...}}

# You (the agent) analyze the skill, produce analysis JSON
run_skill(action="submit", step="analyze_skill", result={"analysis": {...}})

  → {status: "in_progress", step: "design_graph", instruction: "Design a stepflow graph...", tools: {...}}

# You produce stepflow YAML
run_skill(action="submit", step="design_graph", result={"pipeline": "name: ..."})

  → [validate_design auto-runs — stepflow_lint checks the YAML]
  → if passed: {status: "completed", outputs: {...}}
  → if failed: {status: "in_progress", step: "fix_issues",
                 instruction: "...linter feedback with errors and suggestions..."}
```

## Pipeline flow

```
analyze_skill     ← you parse the skill → phases, decisions, tools, checkpoints
    ↓
design_graph      ← you produce stepflow YAML
    ↓
validate_design   ← auto: stepflow_lint checks the YAML
    ↓
  ├─ passed → done → completed
  └─ failed → fix_issues ← you fix errors (linter feedback in instruction)
                  ↓
            validate_fix ← auto: re-check
                  ↓
              ├─ passed → done → completed
              └─ failed → fix_issues (up to 3 attempts)
```

## Step details

### analyze_skill

**Instruction**: "You are a pipeline architect. Analyze the given skill description..."

**You produce**: Analysis JSON:
```json
{
  "analysis": {
    "phases": ["phase_name", ...],
    "decisions": [
      {"condition": "when this happens", "branches": ["branch_a", "branch_b"]}
    ],
    "terminal_condition": "how the skill knows it's done",
    "tools_per_phase": {"phase_name": ["tool", ...]},
    "checkpoints": ["phase_where_human_approval_needed"]
  }
}
```

### design_graph

**Instruction**: "Design a stepflow graph based on the analysis..." Context includes your analysis from the previous step.

**You produce**: Complete stepflow YAML — must follow these rules:
- Every `agent` step has `agent_config`
- Decision points → `gate` steps with `match` transitions
- Cycles need `max_loop`
- Terminal nodes need `end_conditions`
- Checkpoint steps have `checkpoint: true` + checkpoint transitions

### fix_issues

**Instruction**: "Fix the linter errors..." Context includes your broken YAML + **linter feedback**:

```
## Feedback from previous attempt
{"passed": false, "errors": 2, "issues": [
  {"severity": "error",
   "message": "Cycle has no max_loop constraint",
   "location": "steps[2].transitions[1]",
   "suggestion": "Add max_loop: 3 to the transition"}
]}
```

**You produce**: Corrected YAML that passes lint. Up to 3 fix attempts.

## Getting the result

When the converter completes, copy the generated YAML to your skill folder:

```python
from plugins.skill_converter import save_output

path = save_output(sf, tool.run_id, output_file="skills/review/skill_pipeline.yaml")
# → Path("skills/review/skill_pipeline.yaml")

# Now run it:
from stepflow.graph import PipelineGraph
graph = PipelineGraph.from_yaml(str(path))
sf.register_graph(graph)
runner = SkillTool(sf, graph.name)
runner(action="next")
```

The agent's skill folder now has both files:
```
skills/review/
├── skill_description.md     ← input (you wrote this)
└── skill_pipeline.yaml      ← output (converter produced this)
```

## Reference: Stepflow YAML Structure

```yaml
name: "my_skill"
begin: "first_step_id"
end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: "final_step"
      result: "completed"

steps:
  - id: "step_id"
    step_type: agent          # agent | gate | tool | loop
    agent_config: "role_name"
    checkpoint: false
    max_retries: 3
    context:
      - source: { step: "previous_step" }
      - source: { tool: "dir_tree" }
    output:
      mode: "content"
      fixed:
        output_slot: "filename.md"
    transitions:
      - to: "next"                                   # default
      - to: "branch"
        match: { flag_key: value }                   # conditional
      - to: "prev"
        match: { ok: false }
        max_loop: 3                                  # cycle limit
```

### Transition patterns

| Pattern | Use case |
|---------|----------|
| `{to: "next"}` | Default — always taken |
| `{to: "branch", match: {key: val}}` | Gate routing |
| `{to: "handler", match: {_error: true}}` | Error handler |
| `{to: "next", match: {from: checkpoint, value: approved}}` | Checkpoint |
| `{to: "prev", match: {ok: false}, max_loop: 3}` | Review loop |
