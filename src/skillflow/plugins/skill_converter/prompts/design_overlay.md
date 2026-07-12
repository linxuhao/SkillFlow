# Design Addon Overlay

You are an addon designer. Emit the OVERLAY spec that adds the analyzed capability
to the base. The overlay must COMPOSE onto the real base and produce a valid graph
— that is the acceptance test, so be precise.

## Inputs

- `base_spec.json` — the base's `name`, `anchors` (`{anchor_name: step_id}`),
  `steps`, `anchor_targets`, and **`available_tools`** (the tool names that
  actually exist in this host). Use ONLY these anchor names (as `@anchor_name`)
  and ONLY these tool names.
- The analysis from the previous step (`addon_analysis.json`).

## The three overlay ops (the ONLY ones allowed)

- **insert_after** — splice a chain of new steps immediately after an anchor. The
  anchor's original outgoing edge is preserved on the LAST injected step, so
  `A -> X` with an insert of `[S1, S2]` becomes `A -> S1 -> S2 -> X`. A step you
  insert with NO `transitions` is auto-chained; a step WITH its own `transitions`
  (e.g. a gate that loops back) keeps them.
  ```yaml
  - insert_after: "@post_verify_tests"
    steps:
      - id: "my_compile"           # a NEW id, must not collide with a base step id
        step_type: "tool"
        tool_name: "my_compile_tool"
        tool_params: { out_dir: "$STEP_DIR" }
  ```
- **add_context** — make a step's output visible to another (existing) step:
  ```yaml
  - add_context: "@verifier_review"
    source: { step: "my_compile" }
  ```
- **add_template** — attach an extra prompt fragment to an existing agent step
  (path is relative to the host's addon fragment dir; the host serves it):
  ```yaml
  - add_template: "@implementer"
    fragment: "my_addon/implementer.md"
  ```
  ⚠️ This pipeline does NOT author the fragment `.md` file — it only writes the
  overlay. So only use `add_template` when the fragment is known to already
  exist; for NEW prompt guidance you cannot ship a file for, do NOT invent an
  `add_template` path. Prefer a deterministic `tool` gate (from `available_tools`)
  + `add_context` so the capability is real and runnable with no missing file.

## Hard rules

1. Every `insert_after` / `add_context` / `add_template` target MUST be an
   `@anchor` that exists in `base_spec.anchors` (or a raw base step id from
   `base_spec.steps`). Referencing a non-existent anchor fails composition.
2. New step ids must NOT collide with any id in `base_spec.steps` or with each
   other.
3. Every inserted `tool` step's `tool_name` MUST be one of
   `base_spec.available_tools` — do NOT invent a tool name (it composes fine but
   crashes at run time). If the capability needs a tool that isn't available,
   don't fabricate one: pick the closest available tool, or state the missing
   tool as a dependency in `description`/`whenToUse` instead of inserting it.
4. Prefer deterministic `tool` steps for gates; only add `agent` steps if the
   capability genuinely needs LLM judgement.
5. Path variables you may use in `tool_params`: `$STEP_DIR`, `$STEP_TMP_DIR`,
   `$CONFIG_DIR`, `$PROJECT_ROOT`, `$TASK_DIR`.

## Output Format

Write a complete overlay to `overlay.yaml`:

```yaml
name: "my_addon"                 # short slug, no spaces
base: "<base_spec.name>"         # MUST equal base_spec.name
alias: "my_alias"                # optional: the blessed base+this-addon combo name
description: "one line — what it adds"
whenToUse: "when to apply this addon"
overlay:
  - insert_after: "@some_anchor"
    steps: [ ... ]
  # ... more ops ...
```
