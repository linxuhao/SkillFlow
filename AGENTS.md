# CLAUDE.md — Skillflow

Skillflow is a config-agnostic LLM pipeline graph executor. It is a pure Python library with minimal dependencies (PyYAML, ruff). Published on PyPI as `skillflow-py` (check `pyproject.toml` for the current version — bump + publish on every behavior change; the main host consumes it from PyPI, not from this checkout).
Under development, no backward compatibility is needed.

## Project Rules

- **Zero AItelier imports.** Skillflow must never import from `core/`, `api/`, `cli/`, `aitelier/`, `models/`, or `templates/`.
- **Docs ship WITH the behavior change, same commit.** Any change to config-visible surface (new context source / StepNode field / tool semantics / ordering guarantees) updates README's matching section, the CLAUDE.md diagrams, and — if it introduces graph syntax — a fixture in `tests/fixtures/` exercising it (auto-validated by `test_load_all_valid_fixtures`; fixtures are also the linter/converter's example corpus, so an undocumented syntax is one the converter can never generate). Versions 1.5.15–1.5.20 shipped six releases of invisible features before this rule existed; the catch-up was 9f7b096. Deliverable without the ledger entry = not done.
- **All tools are in `src/skillflow/tools/{name}/`** with `tool.yaml` + `impl.py`. Function name must match directory name.
- **Tests in `tests/`** (core) + `src/skillflow/plugins/*/tests/` (plugins). Run: `pytest tests/ src/skillflow/plugins/`
- **Backward compat:** New fields on StepNode/Transition must have defaults. Old YAML without new fields must still parse.
- **`type` field in tool.yaml is forbidden** — tools are callable by both agent steps and tool steps. Access control is via `agent_config.tools: [...]` (for agents) and `tool_name: "..."` (for tool nodes).

## Build & Test

Project uses `.venv/` at repo root (auto-detected by VS Code). First-time setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ~/skillflow[dev]
```

Then:

```bash
pytest tests/ -v                    # core tests
pytest src/skillflow/plugins/ -v    # plugin tests (linter, skill_runner, skill_converter)
```

## Architecture

```
PipelineGraph (YAML) → GraphResolver (validation + traversal)
                         ↓
SkillFlow (orchestrator)  ← SQLite (WAL mode)
  ├── claim_next_step()  → ClaimedStep → StepRunner (host app)
  ├── confirm_step()     → completed/failed
  ├── fail_step(retryable=True) → pending same producer; retain retry cursor
  ├── advance_run()      → resolve gates, auto-execute tools
  │   ├── recover_stale_claims() (built-in) — ownership decides: a dead owner
  │   │     (identity.py) is reclaimed at once, a LIVE one never is however
  │   │     long it is quiet, and only an unprobeable owner falls to the
  │   │     silence lease; a reclaimed executor is fenced by claim_epoch
  │   └── feedback loopback (inject tool error into step inputs)
  ├── _routing_reason_suffix() → on a terminal routing failure (cycle limit /
  │     no matching transition, agent+tool+gate, incl. the pre-resolved gate
  │     path), re-read the edges' from_file targets and put their human-readable
  │     field in error_reason BEFORE the edge detail (hosts truncate for status
  │     chips). null fields skipped. Bounded (3 files / 300 chars), never raises.
  ├── get_checkpoint() → retained outputs + exact expected checkpoint identity
  ├── approve_checkpoint(expected_checkpoint, decision_id) → atomic CAS + durable replay-safe approval event
  ├── reject_checkpoint() → fresh non-loop revision identity; loop-body rejection refused
  └── drain_outbox()     → event stream

ReadTools / native read_file → one original byte snapshot after path checks
  # file_byte_sha256 + byte_size describe that whole snapshot; text still uses
  # existing UTF-8/newline/window policies. Byte digest is not citation.sha
  # or edit authorization and does not promise future file stability.
  # Unified read/search/list apply resolved-root containment and the
  # protected credential filename policy to direct, recovered and enumerated
  # paths. In-root links, ordinary dotfiles and explicit .env examples remain
  # readable. Native read_file shares that protected-file policy. Both lexical
  # and resolved rooted identities retain .aws/.ssh when source roots narrow.

ToolLoader (multi-source)
  ├── Native: src/skillflow/tools/
  └── Custom: host app adds via add_tools_dir()

ContextResolver → assemble prompt context from:
  ├── {config, output}  (cross-config)
  ├── {step, output, mode, scope}  (same-config; scope=task|all routes a LOOP-BODY
  │     producer's per-item output {step}/{item}/ — same-loop reader gets its own
  │     item, outside reader/scope:all gets every item; see workspace.route_step_read_dir)
  ├── {from: repository, path|mode: tool}  (CODE repo — inline needs path:, pathless inline refused)
  ├── {feedback_of: step}  (accumulated checkpoint-feedback log + read contract; volatile tier)
  └── {tool}  (dynamic call)
  # constructed with code_root=get_project_code_path so repository/context-tools
  # see the same tree the read tools serve (NOT workspace/"project")

StepValidator → run validation specs: [{files, tool, inline_schema}]
  # claim run_id/step_id + resolver project_root are explicit per-instance
  # tool_context; supported host identity overwrites spec values. Native lint
  # forwards supported keywords to custom backends; Path-only callers remain.
WriteTools → generate constrained write_*/create_*/edit_* tools from output.fixed
  # missing/null content is a tool error before file creation/replacement/archive;
  # explicit empty fixed text is supported, JSON validation remains unchanged.
  # code-target destinations → shared Git ignore preflight before mutation /
  # ownership; native apply_patch guards its entire write batch the same way.
  # Tracked ignored files stay writable; artifact append behavior is unchanged.
  # edit baseline: staging → repo → own promoted dir (same-run gated)
  # staging FIRST so a step's successive edits COMPOUND; the repo copy is only
  # the pre-step baseline (repo_apply refreshes it after the step ends)
```

## Key Data Structures

- `Transition(to, match, max_loop, label, feedback)` — directed edge
- `StepNode(id, step_type, transitions, checkpoint, checkpoint_reject_to, config, tool_name, tool_params, agent_config, context, output_mode, output_fixed, validation)` — graph node (checkpoints work on tool steps too)
- `ClaimToken(step_id, run_id, step_instance_id, version, claimed_at, claim_epoch)` — frozen claim.
  `version` = optimistic concurrency (*reload and re-decide*); `claim_epoch` = fencing token,
  bumped by every claim and checked by `confirm_step`/`fail_step`/`execute_tool` (*stop*).
- `ClaimedStep(token, step_config, run_context, inputs, validation_error)` — ready to execute
- `StepResult(outputs, flags)` — execution result (flags used for transition matching)
- `StepRunner` — Protocol: `async def execute(step: ClaimedStep) -> StepResult`

## Tools

Each tool: `tool.yaml` (schema) + `impl.py` (function). Function signature:

```python
def tool_name(*params, *, workspace_root: str = "", project_root: str = "") -> dict:
    ...
    return {"verdict": "passed"} | {"verdict": "failed", "feedback": "..."}
```

Native tools (13): read_file, write, list_tree, dir_tree, json_schema, syntax_lint, py_compile, pytest, repo_apply, repo_validate, draft_commit, file_exists, notify.

## CLIs (console scripts)

- `skillflow-run` — **runner mode**: an external agent drives a pipeline step-by-step over a stateless CLI (host-delegated; agents declared `model: "host"`). Bring-your-own-agent — any CLI agent (Claude CLI, Codex, …) can drive a pipeline and inherit the trust properties.
- `skillflow-convert` — turn a plain-language skill/workflow description into a runnable SkillFlow pipeline YAML.
- `skillflow-lint` — validate a pipeline YAML (graph cycle-safety, reachability, schema).

## Plugins (`src/skillflow/plugins/`)

- `linter` — pipeline-YAML linter; also exposed as the `skillflow_lint` tool + the `skillflow-lint` CLI.
- `skill_runner` — host-delegated runner mode. Three transports over one core: `RunnerService` (`service.py` — in-process embed: start guard, seeding, stateless reconnect, `execute_step_tool` proxy with host-tool redirect, finish_step→submit mapping), the `skillflow-run` CLI, and `skillflow-mcp` (`mcp_server.py`, typed MCP tools over stdio; optional extra `skillflow-py[mcp]`). PromptAssembler emits a transport-neutral slot contract — never advertise write_*/finish_step as directly-callable functions.
- `skill_converter` — skill→pipeline graph (host-mode agents) behind `skillflow-convert`; AItelier registers it to power its `generate_pipeline` butler tool.

## Trace connection ownership

`SkillFlow.trace_connection(project_id)` borrows under the engine RLock for the
complete SQL transaction/cursor use. The per-project idle LRU is bounded to 32;
actual nested borrowers pin their entries, with invalidation deferred until
release. Trace/query APIs use the same scope. Raw connections/cursors must not
escape it; hosts must migrate from the removed private `_get_trace_conn`.
The unpublished `1.5.81+trace1` candidate requires coordinated host adaptation.

## apply_patch hands back coordinates (1.5.83)

A refused reference carries `fresh` (current text of the lines it named + a
sha for exactly that text) for stale, outside-window, never-issued-sha and
missing-sha cases; a successful write's `echo` entries carry `citation` /
`citations` in the file's current line numbers. Both are issued through
`citations.issue` after `_journal`, so they frame the file at the generation
the write produced. The refusal wording is unchanged; only the payload grew.
See README "The native `apply_patch(patch)` tool" and
`tests/test_a_write_hands_back_the_coordinates.py`.

## Host integration

- `WorkspaceManager(code_path_resolver=…)` — optional hook mapping a project to a host-managed code path (e.g. an existing repo), so `repo_apply` commits into the real repo. `SkillFlow(code_path_resolver=…)` forwards it.


## Output destinations: artifact or code

Agent `output.target` defaults to `artifact`; `code` writes directly to the
resolver-owned Git worktree, independently of `output.mode`. Fixed slots may
override target; mixed outputs use default artifact with explicit code slots.

```yaml
output:
  mode: content
  target: artifact
  fixed:
    plan: {file: plan.md, target: artifact}
    readme: {file: README.md, target: code}
```

Code outputs have no tmp directory, read overlay, promotion, deferred deletion,
or repo_apply hook. Validation runs against changed candidate paths in code;
failed validation never promotes code. The engine records path metadata outside
the repository, commits only tracked output paths, and publishes a
`code_changes.json` artifact receipt with base/candidate commit. Artifact-only
publication remains backward-compatible. New tasks require a clean code tree;
resume retains the owning step's pending changes, and review revisions of the
same task preserve its original base. No automatic reset or rollback is performed.

Do not point STEP_TMP_DIR at a worktree. Do not use code targets for tool nodes: a
tool's effects are explicit in its implementation. Custom agent tools declare
`output_dir` and `output_target` keyword parameters and return actual changed
paths under `written`/`edited`/`created`/`deleted`/`removed`; partial failures must
report their successful subset as well as `error`. The engine injects destinations
and accounts for those paths. Opaque external writers are not implicitly trusted.

A host can seed a failed-code recovery with artifact metadata
`.code-output-relay.json` (`base_commit`, `recovery_commit`, `steps` mapping step
ids to inherited paths). The engine verifies Git ancestry and subjects the
inherited paths to normal validation; recovery is not approval. Hosts must
quiesce the old run and preserve provenance before producing that recovery commit.

The private local-version build `1.5.72+aitelier.output1` is for the coordinated
AItelier migration; no public package publication is implied.

## Read byte identity (SOURCE candidate)

Unified read and native read_file return file_byte_sha256 and byte_size for the
complete original byte snapshot, including partial/clipped/raw/outline reads.
The snapshot is decoded using the prior text policies; path permissions, source
precedence, edit citations and read accounting are unchanged. Byte identity is
not an edit citation or a guarantee of future file stability. This candidate
does not bump the release version, publish, or change an installed engine.

## Generic source read boundary (SOURCE candidate)

Unified read/search/list reuse source-root containment for basename recovery
and enumeration. The new explicit credential filename policy is documented in
README's Read Surface section; it applies before byte/text access and checks
both lexical and resolved rooted paths, including narrowed source containers.
Native read_file uses that same policy before its snapshot read. Tests use
actual native claims and only owned synthetic populated
credentials and outside-root fixtures. This source candidate does not bump,
publish, install or deploy an engine.

## Early ignored code output refusal (SOURCE candidate; iss-78f25187ffd64730)

Generated code-target write/create/edit tools and native apply_patch Add/Update/
reference operations use one Git delivery preflight before filesystem or output
ownership mutation. Refusals name the matching ignore source, line and pattern
and allow a corrected path in the same claim. Git index semantics preserve
already-tracked ignored files; artifact writes and declared artifact appends
are unaffected. Missing/non-Git runtime failures remain errors. Existing final
delivery checks remain in place. README's output-destinations section and
tests/test_code_write_ignore_preflight.py describe and exercise this change;
no graph syntax was added. Prepared as package 1.5.90 (docs/releases/1.5.90.md);
that preparation does not publish, install or deploy; publication, pin and
deployment decisions and independent review belong to the coordinator.


## Optional custom lint result data (1.5.92)

Native lint preserves a custom backend's optional JSON mapping `details` in the
same per-file normalized result, using a detached JSON copy. Invalid/nonmapping
data is omitted without changing the actual lint outcome. Ordinary optional-data
retrieval/conversion errors are isolated; cancellation/BaseException propagates. Built-in/no-op and
caught-exception results do not gain details. This is caller data, not authority;
dispatch, defaults, host context and ownership policy are unchanged. See README
"Output Validation" and tests/test_lint_result_details.py.

## Runner protocol fixes and pipeline overview (1.5.93)

CLI submit stages result slots through RunnerService.submit (as MCP does), so
validated content steps and skillflow-convert work from the CLI; rejected slot
values return validation_error. Every runner instruction is prefixed with a
whole-pipeline overview from the run's pinned graph (current step marked,
windowed beyond 30 steps); routing is unchanged. The mcp extra excludes mcp 2.x
(FastMCP removed) and --graphs dirs skip their agents.yaml. See
docs/releases/1.5.93.md, README "Runner Mode" and the skill_runner tests.

## Retained checkpoint approval identity (SOURCE candidate)

Before checkpoint confirmation, literal fixed slots are captured from their
framework-resolved artifact/code destinations; JSON remains structured. Missing,
malformed or escaped slots cannot complete. Carried slots and CodeOutput delivery
keep their existing lifecycle. No model-provided path selects a read.

`get_checkpoint` exposes retained outputs and the completed instance/version/
completion sequence/canonical outputs SHA256. `approve_checkpoint` requires that
exact expected identity and a durable unique decision id. One SQLite transaction
verifies actual pinned graph history/digest and the identity-bound routed
`checkpoint_paused` event, records its approval outbox event and transitions
the paused run. The routing event is recorded atomically at both actual agent
and inline-tool checkpoint paths. Generic confirm→manual-pause without routing
cannot authorize a checkpoint. Execution's best-effort resolver stays separate. Exact decision replay is inert; collisions and stale identities
refuse. Normal outbox ack retains approval identity; deleting run history removes
it. RunnerService, MCP and CLI forward the explicit pair and return `approved`;
`next` requests the next instruction separately, so replay cannot reclaim a later
worker. Hosts authenticate actual human decisions separately. This private Source
candidate does not bump, publish, install or deploy an engine. This private Source composition includes official1.5.93 runner slot/overview
behavior and the independently accepted scoped recovery/Git/checkpoint changes.


## Closed engine Git consumers (SOURCE candidate; sdk.code-output-git-cannot-run-repository-programs)

`skillflow/closed_git.py` is the only engine path to Git. CodeOutput write
preflight/prepare/assert_published/commit, repo_apply, draft_commit, artifact
history and git_sync_pre use its closed profile, which drops inherited GIT_*
routing, turns system config off, replaces global config with identity/
safe.directory/excludes data, and overrides hooks/fsmonitor/signing/diff/
credential/transport. They also use its raw no-follow file hashing and
private-index publish: owned index.lock, unsigned hook-free commit-tree,
journal pending_commit, HEAD compare-and-set, then index install. Converting
attributes (filter/encoding/ident/CRLF), unmerged entries and git_sync_pre
pull are refused visibly, with no native fallback. Existing clean-tree,
unrelated-change, receipt and add -A semantics are preserved. Do not add a
native `git add`/`status`/`commit`/`diff`-against-worktree call to the engine.
See README "Closed Git" and tests/test_git_consumers_run_no_repository_programs.py.
This source candidate does not bump, publish, install or deploy an engine.


## Complete runner instruction composition (1.5.94 Source preparation)

The default pinned overview policy also applies to checkpoint-paused instructions;
explicit custom assembler opt-out remains effective. This private composition
retains accepted scoped claim recovery, retained checkpoint CAS and closed Git
behavior while preserving CLI/MCP slot staging and invalid-slot feedback.
Legacy test drivers now pass retained approval identities and request next after
an approved response; their tested graph outcomes are unchanged. See
`docs/releases/1.5.94.md`. Source preparation is not release or installation.

## Rejected checkpoint retry cursor (SOURCE candidate)

Retryable executor/confirmation failures retain the failed producer's current
node. Reclaim keeps the revision instance and retry history; an earlier completed
checkpoint cannot be routed again while this producer is pending. Fixed-slot
capture, completion ordering and approval identity checks remain unchanged.
See README "Checkpoints" and tests/test_rejected_checkpoint_retry_cursor.py.
This Source candidate does not bump, publish, install or deploy the engine.
