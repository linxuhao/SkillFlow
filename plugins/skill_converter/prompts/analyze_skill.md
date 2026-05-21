# Analyze Skill Description

You are a pipeline architect. Analyze the given skill description and identify its structural components.

## Instructions

1. **Identify phases**: What are the sequential stages/steps the skill goes through?
2. **Identify decision points**: Where does the next step depend on the output of a previous step?
3. **Terminal condition**: How does the skill know it's done?
4. **Tools per phase**: What tools does each phase need?
5. **Checkpoints**: Are there stages where a human should review before proceeding?

## Output Format

Output a JSON file `skill_analysis.json` with this structure:

```json
{
  "phases": ["phase_name", ...],
  "decisions": [
    {
      "condition": "description of the condition",
      "branches": ["branch_a", "branch_b"]
    }
  ],
  "terminal_condition": "description of when the skill is complete",
  "tools_per_phase": {
    "phase_name": ["tool_name", ...]
  },
  "checkpoints": ["phase_name", ...]
}
```
