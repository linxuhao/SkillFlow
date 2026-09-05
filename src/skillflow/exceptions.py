"""skillflow exceptions.

All skillflow-specific exceptions inherit from SkillFlowError so callers
can catch a single base type or specific subtypes.
"""


class SkillFlowError(Exception):
    """Base exception for all skillflow errors."""


class StepVersionConflict(SkillFlowError):
    """Optimistic concurrency failure.

    Raised when confirm_step or fail_step finds the step's version has
    changed since it was claimed (e.g. stale claim recovery reset it).
    The caller should discard the result and let the next tick re-claim.
    """


class StaleClaimFenced(StepVersionConflict):
    """The caller's claim_epoch is not the step's — it is no longer the executor.

    A ``StepVersionConflict`` says *reload and re-decide*. This says **stop**:
    the step was reclaimed (crash recovery, stale lease) and someone else is
    executing it now. Nothing the caller re-reads will change that, so a retry
    would be a second live writer on the same step. Subclasses
    StepVersionConflict so hosts that already handle a lost claim keep working.
    """


class TerminalRunFenced(SkillFlowError):
    """The run is terminal — this write was refused before it could take effect.

    Distinct from :class:`StaleClaimFenced`, and the distinction is the whole
    point: a stale claim means *someone else is executing this step*, so the
    caller lost a race with a peer. This means *the run is over* — cancelled
    (``fail_run``) or finished — so there is no peer and nothing to re-decide.

    Raised by ``_admit_op`` — from ``confirm_step`` immediately before the
    lifecycle hooks, and from ``execute_tool`` immediately before the tool — which
    is the last instant at which promotion, ``on_deliver`` (``repo_apply``: real
    git commits) or a tool write can still be prevented. Also raised while a run
    is DRAINING a requested cancellation: admitted work finishes, nothing new
    starts.

    Deliberately NOT a ``StepVersionConflict``: a host that reacts to a lost
    claim by re-claiming would, on a cancelled run, be re-entering something the
    operator just stopped.

    On the cancelling side, ``stop_run`` reports what it could not prevent as
    ``admitted_operations``. Admitted is not running — an admitted operation has
    been allowed to proceed and can no longer be called off, which is a weaker
    and truer statement than saying its effect is already under way.
    """


class RequiredContextMissing(SkillFlowError):
    """A context source marked ``required: true`` resolved to no content.

    Raised by ContextResolver when a required input (e.g. a project brief the
    step cannot meaningfully run without) is missing or empty. The step must
    fail loudly rather than run on absent context and hallucinate — so callers
    should let this propagate (fail the step) instead of swallowing it like a
    best-effort resolution miss.
    """


class CycleLimitExceeded(SkillFlowError):
    """A transition's max_loop limit has been reached.

    Raised during graph traversal when all valid transitions from a node
    are exhausted due to max_loop constraints.
    """


class GraphValidationError(SkillFlowError):
    """Pipeline graph structure is invalid.

    Raised at graph registration time. The ``issues`` list contains
    human-readable descriptions of each validation failure.
    """

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("\n".join(issues))


class OutputValidationError(SkillFlowError):
    """Step output failed schema validation.

    Raised by OutputValidator when a StepResult's outputs don't conform
    to the step's output_schema Pydantic model. The caller should feed
    the error message back to the LLM and re-claim the step.
    """


class ToolArgumentsUnavailable(SkillFlowError):
    """A tool step declares a parameter the engine cannot supply.

    Raised by the tool-step path when ``signature.bind`` shows the call cannot
    be made at all — e.g. ``git_sync_pre(project_root: str)`` on a run whose
    code-path resolver answers "this run owns no code repository", so the engine
    deliberately supplies no ``project_root``.

    Deterministic by construction: the graph, the step and the missing argument
    are all the same on the next tick, so retrying reproduces it exactly. The
    caller therefore fails the step AND the run instead of reopening the step to
    pending — and must never confirm it as completed, which would record a step
    that never ran and route the run down the node's first edge.
    """


class IsolationUnavailable(SkillFlowError):
    """A run declares an isolated code root and the engine cannot resolve it.

    The point of this class is that it must NOT be caught by the best-effort
    root filling around it. Two call sites resolve ``project_root`` inside
    ``except Exception:`` blocks that log and continue, which is right for "the
    lookup hiccuped, use the default" and catastrophic for "this run's tree is
    gone": continuing means the step runs against whatever root the fallback
    invents — for a run that declares isolation, the shared checkout it was
    isolated FROM. There is no correct silent answer, so this one is raised and
    re-raised, and the step fails naming the run.

    Never used for a repo-LESS run. "This run owns no repository" is an answer
    (``False`` from the code-path resolver) and is handled by omitting the
    argument; this is the absence of an answer that was promised.
    """


class NoMatchingTransition(SkillFlowError):
    """No transition matched the step's result flags.

    Raised when a step completes but none of its transitions match
    (and no error transition exists).
    """
