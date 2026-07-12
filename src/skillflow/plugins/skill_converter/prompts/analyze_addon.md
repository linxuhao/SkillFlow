# Analyze Addon Request

You are an addon architect. You are given a capability to ADD to an existing base
pipeline, plus that base's extension points. Decide WHAT to inject and WHERE.

## Inputs

- `addon_description.md` — the capability to add (an engine gate, an i18n pass, a
  mobile harness, an extra review stage, …).
- `base_spec.json` — the base graph's extension surface:
  - `name` — the base graph name.
  - `anchors` — `{anchor_name: step_id}`. These are the ONLY legal injection
    points. Refer to them by `@anchor_name`.
  - `steps` — the base's step ids (for context; do NOT invent new anchors).
  - `anchor_targets` — `{anchor_name: [step_id, ...]}` — where each anchor's step
    currently transitions, so you understand what an inserted step sits between.

## Instructions

1. **Intent** — one line: what capability does this addon add?
2. **Injections** — for each thing to add, pick a REAL anchor from `base_spec.anchors`
   and the op kind:
   - `insert_after` — splice a new step (usually a `tool` gate) after an anchor.
   - `add_context` — make an injected/base step's output visible to another step
     (e.g. so a reviewer reads a new gate's report).
   - `add_template` — attach an extra prompt fragment to an existing agent step
     (guidance that reaches the agent ONLY when this addon is applied).
3. **Tools** — any tool names new `insert_after` gate steps will call.
4. Only use anchors that exist in `base_spec.anchors`. Never invent one.

## Output Format

Write `addon_analysis.json`:

```json
{
  "intent": "one-line capability summary",
  "injections": [
    {"anchor": "post_verify_tests", "what": "add a compile gate", "kind": "insert_after"}
  ],
  "tools": ["some_gate_tool"],
  "context_additions": ["make the compile report visible to verifier_review"],
  "template_fragments": ["engine conventions for the implementer"]
}
```
