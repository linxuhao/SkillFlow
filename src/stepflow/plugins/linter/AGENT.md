# stepflow_lint — Pipeline Config Validator

Validate a stepflow pipeline YAML config and return detailed, fixable issues.

## Tool Schema

```json
{
  "name": "stepflow_lint",
  "description": "Validate a stepflow pipeline YAML file or raw YAML content. Returns detailed issue reports with locations and fix suggestions.",
  "parameters": {
    "path": {
      "type": "string",
      "description": "Path to the stepflow YAML config file to validate",
      "required": false
    },
    "content": {
      "type": "string",
      "description": "Raw YAML content as a string (alternative to path)",
      "required": false
    }
  }
}
```

## Usage

Provide either `path` or `content`:

```
stepflow_lint(path="/path/to/pipeline.yaml")
stepflow_lint(content="name: my_pipeline\nbegin: start\n...")
```

## Output Format

```json
{
  "passed": false,
  "errors": 2,
  "warnings": 1,
  "issues": [
    {
      "severity": "error",
      "message": "Step 't_plan': transition target 't_verify' not found in graph",
      "location": "steps[5].transitions[0].to",
      "suggestion": "Rename the target or add a step with id 't_verify'"
    },
    {
      "severity": "error",
      "message": "Cycle detected: reviewer → writer → reviewer has no max_loop constraint",
      "location": "steps[2].transitions[1]",
      "suggestion": "Add max_loop: 3 to the transition from 'reviewer' to 'writer'"
    },
    {
      "severity": "warning",
      "message": "Step 'orphan' is unreachable from begin 'analyze'",
      "location": "steps[6]",
      "suggestion": "Add a transition to 'orphan' or remove the step"
    }
  ]
}
```

- `passed`: `true` if there are zero errors (warnings don't block)
- `errors`: count of error-severity issues
- `warnings`: count of warning-severity issues
- `issues`: list of findings, each with `severity`, `message`, `location`, `suggestion`

## What gets checked

| Check | Severity |
|-------|----------|
| `begin` field is present and references a real step | error |
| No duplicate step IDs | error |
| All transition `to` targets exist | error |
| Every cycle has at least one `max_loop` edge | error |
| No unreachable steps | error |
| YAML syntax is valid | error |

## When to use

- After generating a stepflow YAML config, validate it before registering
- In a fix loop: lint output → fix issues → lint again → repeat until passed
- As a pre-commit check before pushing pipeline configs
