# Fix Addon Overlay

The overlay you designed FAILED to compose-validate. The `compose_validate` tool
tried to apply it to the real base graph and reported errors. Fix the overlay so
it composes cleanly and the resulting graph is valid.

## Inputs

- `base_spec.json` — the base's `name`, `anchors`, `steps` (re-check every anchor
  and step id against these).
- The current overlay from the design_overlay step (`overlay.yaml`).
- The compose report (`compose_report.json`) with the `errors` list — address
  EVERY error.

## Common fixes

- **"unknown anchor '@x'"** — the anchor is not in `base_spec.anchors`. Use a real
  one, or target a raw base step id from `base_spec.steps`.
- **"inserted step id 'y' collides"** — rename the new step to an id not in
  `base_spec.steps`.
- **"overlay binds to base '…'"** — set `base:` to exactly `base_spec.name`.
- **reachability / cycle / transition errors** — a gate you inserted loops back
  without `max_loop`, or a `to:` points at a missing step. Fix the wiring.

## Output Format

Write the corrected complete overlay to `overlay.yaml` (same schema as the
designer: `name`, `base`, optional `alias`, `description`, `whenToUse`,
`overlay: [ops]`). Change only what the errors require.
