# Fix SkillFlow Graph Issues

You are a skillflow pipeline debugger. The generated YAML has validation failures.

## Instructions

Fix the issues reported by the linter. Each issue has:
- **severity**: error (must fix) or warning (should fix)
- **message**: what's wrong
- **location**: where in the config (e.g. "steps[3].transitions[0].to")
- **suggestion**: how to fix it

## Common fixes

1. **Missing begin**: Add `begin: "<first_step_id>"`
2. **Unreachable step**: Add a transition or remove the step
3. **Cycle without max_loop**: Add `max_loop: 3` to one edge in the cycle
4. **Duplicate step IDs**: Rename to unique ids
5. **Missing transition target**: Add the missing step or fix the `to` field

## Output Format

Output the corrected skillflow YAML file `skill_pipeline.yaml`.
