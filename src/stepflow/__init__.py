"""stepflow — Transactional graph orchestrator for Python.

A standalone framework for defining and executing pipeline graphs
with transactional state management on SQLite. Zero external
dependencies beyond Python 3.12 stdlib.

Usage::

    from stepflow import StepFlow, PipelineGraph, StepRunner, StepResult

    sf = StepFlow(":memory:")
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

from stepflow.exceptions import (
    StepFlowError,
    StepVersionConflict,
    CycleLimitExceeded,
    GraphValidationError,
    OutputValidationError,
    NoMatchingTransition,
)

from stepflow.graph import (
    Transition,
    StepNode,
    EndCondition,
    EndConditions,
    EndResult,
    PipelineGraph,
    GraphResolver,
)

from stepflow.core import (
    StepFlow,
    StepRunner,
    ClaimToken,
    ClaimedStep,
    StepResult,
    OutboxEvent,
)

from stepflow.validation import OutputValidator
from stepflow.outbox import OutboxConsumer
from stepflow.recovery import recover_stale_claims
from stepflow.notifications import NotificationBus, Notification
from stepflow.agent_registry import AgentRegistry, AgentConfig

__all__ = [
    # Main class
    "StepFlow",
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
    "StepFlowError",
    "StepVersionConflict",
    "CycleLimitExceeded",
    "GraphValidationError",
    "OutputValidationError",
    "NoMatchingTransition",
]
