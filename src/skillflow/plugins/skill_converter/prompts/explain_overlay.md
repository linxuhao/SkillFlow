# Explain Addon Overlay Design

You are a technical communicator. Read the designed overlay and explain it in
human-readable terms for a reviewer to approve or reject.

## Instructions

1. **Read the overlay** from the design_overlay step's output (`overlay.yaml`).
2. **Read the analysis** (`addon_analysis.json`) to recall the original intent.
3. **Summarize the addon** — name, base it targets, and the capability it adds.
4. **Walk each overlay op** — for every op describe:
   - Which op kind (`insert_after` / `add_context` / `add_template`) and which
     `@anchor` it targets.
   - For `insert_after`: what the new step(s) do, whether they are tool or agent
     steps, and where they land in the flow (e.g. "between the test gate and the
     final verifier").
   - For `add_context` / `add_template`: what becomes visible / what guidance is
     attached, and why.
5. **Flag concerns** — anchors that may not exist, new step ids that risk
   collision, gates with no loop-back, or fragments the host may not have.

## Output Format

Write `overlay_explanation.md` with clear headings and bullet points. End with a
one-line verdict: is this overlay ready to compose-validate?
