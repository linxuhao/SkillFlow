# Cited insertion points and bounded remap history

A point citation has no characters of its own. Placement is no longer
corroborated when an earlier journaled replacement or deletion consumes its
immediate right neighbor, or ends at the point and consumes its immediate
left neighbor. Such references refuse before publication and name the path,
reason, and reread remedy. An insertion exactly at an old point remains
ambiguous. Edits elsewhere translate unchanged points and ranges.

Successful `apply_code_patch` responses for a run include `remap_history`, one
entry per written file. `retained_generations`, `max_generations`, `max_files`
and `max_runs` describe current bounded coverage. No retained generations
produces an explicit warning that older-chain citations require a reread.
This is coverage, not a promise that every citation is valid: external writes,
replaced text, file/run eviction, or a generation limit reset can refuse a
citation. Generation overflow discards the whole chain rather than renumbering
still-resident citations. Defaults remain 4096 generations per file, 256 files
per run, 64 runs and 1024 citations per run.
