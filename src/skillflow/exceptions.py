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


class NoMatchingTransition(SkillFlowError):
    """No transition matched the step's result flags.

    Raised when a step completes but none of its transitions match
    (and no error transition exists).
    """
