"""skillflow — Transactional graph orchestrator for Python.

A standalone framework for defining and executing pipeline graphs
with transactional state management on SQLite. Only depends on
PyYAML.

Usage::

    from skillflow import SkillFlow, PipelineGraph, StepRunner, StepResult

    sf = SkillFlow(":memory:")
    graph = PipelineGraph.from_yaml("pipeline.yaml")
    sf.register_graph(graph)

    run_id = sf.create_run("my_pipeline", {"project_id": "X"})
    sf.start_run(run_id)

    while True:
        next_node = sf.advance_run(run_id)
        if next_node is None:
            break
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            continue
        result = await runner.execute(claimed)
        sf.confirm_step(claimed.token, result)
"""

# ── Public API re-exports ──────────────────────────────────────────

from skillflow.exceptions import (
    SkillFlowError,
    StepVersionConflict,
    CycleLimitExceeded,
    GraphValidationError,
    OutputValidationError,
    NoMatchingTransition,
)

from skillflow.graph import (
    Transition,
    StepNode,
    EndCondition,
    EndConditions,
    EndResult,
    PipelineGraph,
    GraphResolver,
)

from skillflow.core import (
    SkillFlow,
    StepRunner,
    ClaimToken,
    ClaimedStep,
    StepResult,
    OutboxEvent,
)

from skillflow.validation import OutputValidator
from skillflow.outbox import OutboxConsumer
from skillflow.recovery import recover_stale_claims
from skillflow.notifications import NotificationBus, Notification
from skillflow.agent_registry import AgentRegistry, AgentConfig

__all__ = [
    # Main class
    "SkillFlow",
    # Graph model
    "PipelineGraph",
    "GraphResolver",
    "StepNode",
    "Transition",
    "EndCondition",
    "EndConditions",
    "EndResult",
    # Protocol & values
    "StepRunner",
    "ClaimToken",
    "ClaimedStep",
    "StepResult",
    "OutboxEvent",
    # Utilities
    "OutputValidator",
    "OutboxConsumer",
    "recover_stale_claims",
    "NotificationBus",
    "Notification",
    "AgentRegistry",
    "AgentConfig",
    # Exceptions
    "SkillFlowError",
    "StepVersionConflict",
    "CycleLimitExceeded",
    "GraphValidationError",
    "OutputValidationError",
    "NoMatchingTransition",
]
