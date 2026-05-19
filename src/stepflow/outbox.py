"""Outbox consumer.

Polls the stepflow_outbox table for pending events and delivers them
to the application's event handler (e.g. SSE streaming).
"""

from __future__ import annotations

from stepflow.core import StepFlow


class OutboxConsumer:
    """Polls and delivers outbox events.

    Usage::

        consumer = OutboxConsumer(stepflow)
        events = consumer.drain(100)
        for event in events:
            await send_to_sse(event)
        consumer.ack([e.id for e in events])
    """

    def __init__(self, stepflow: StepFlow):
        self._sf = stepflow

    def drain(self, batch_size: int = 100):
        """Claim pending events. Returns them with status 'draining'."""
        return self._sf.drain_outbox(batch_size)

    def ack(self, event_ids: list[int]) -> None:
        """Mark events as delivered."""
        self._sf.ack_outbox(event_ids)
