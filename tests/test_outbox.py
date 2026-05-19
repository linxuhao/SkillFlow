"""Unit tests for outbox.py."""

import pytest

from stepflow.core import StepFlow
from stepflow.graph import PipelineGraph, StepNode
from stepflow.outbox import OutboxConsumer


def _agent(id: str):
    return StepNode(id=id, step_type="agent")


def test_consumer_drain_returns_events(sf: StepFlow):
    graph = PipelineGraph(name="test", begin="a", steps=[_agent("a")])
    sf.register_graph(graph)
    sf.create_run("test")

    consumer = OutboxConsumer(sf)
    events = consumer.drain(10)
    assert len(events) > 0
    assert events[0].event_type == "run_created"


def test_consumer_ack_marks_delivered(sf: StepFlow):
    graph = PipelineGraph(name="test", begin="a", steps=[_agent("a")])
    sf.register_graph(graph)
    sf.create_run("test")

    consumer = OutboxConsumer(sf)
    events = consumer.drain(10)
    ids = [e.id for e in events]

    consumer.ack(ids)

    # Second drain should return empty
    events2 = consumer.drain(10)
    assert len(events2) == 0


def test_consumer_events_ordered_by_id(sf: StepFlow):
    graph = PipelineGraph(name="test", begin="a", steps=[_agent("a")])
    sf.register_graph(graph)
    sf.create_run("test")
    sf.create_run("test")  # Second run for more events

    consumer = OutboxConsumer(sf)
    events = consumer.drain(50)
    ids = [e.id for e in events]
    assert ids == sorted(ids)


def test_consumer_ack_empty(sf: StepFlow):
    consumer = OutboxConsumer(sf)
    consumer.ack([])  # Should not raise


def test_consumer_respects_batch_size(sf: StepFlow):
    graph = PipelineGraph(name="test", begin="a", steps=[_agent("a")])
    sf.register_graph(graph)
    # Create several runs to generate multiple events
    for _ in range(5):
        sf.create_run("test")

    consumer = OutboxConsumer(sf)
    events = consumer.drain(batch_size=2)
    assert len(events) <= 2
