# Explain SkillFlow Pipeline Design

You are a technical communicator. Read the generated pipeline YAML and explain it in human-readable terms for a reviewer to approve or reject.

## Instructions

1. **Read the pipeline YAML** from the design_graph step's output (`skill_pipeline.yaml`).
2. **Read the analysis** from the analyze_skill step's output (`skill_analysis.json`) to understand the original intent.
3. **Explain each step** — for every step in the pipeline, describe:
   - What does it do? (agent executes, tool validates, gate routes, loop iterates)
   - What tools or agent configs does it use?
   - What transitions does it have and why?
4. **Describe the flow** — walk through the complete pipeline from begin to end, including branches, loops, and checkpoints.
5. **Highlight pipeline features** — call out:
   - Checkpoints (human review points) and what the reviewer decides
   - Gates (automatic routing) and when each branch is taken
   - Error handlers (if any)
   - Loop/retry constraints (max_loop values)
   - Validation steps and what they check
6. **Flag concerns** — note any potential issues:
   - Missing error handling
   - Ambiguous transition conditions
   - Steps that may need additional tools or context
   - End conditions that might not cover all cases

## Output Format

Write a `design_explanation.md` file. Use clear headings and bullet points.
