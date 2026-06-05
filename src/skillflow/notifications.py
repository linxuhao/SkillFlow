"""Notification bus — publish/subscribe for pipeline events.

SkillFlow provides an outbox-backed default. Host apps (like AItelier)
inject a transport (SSE, WebSocket, in-process callback) by registering
a subscriber on the bus.

Events flow:
  skillflow internals → NotificationBus.publish()
    ├── outbox table (persisted, for polling consumers)
    └── subscriber callbacks (push, for real-time consumers)
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol


# ── Event dataclass ────────────────────────────────────────────────────

@dataclass
class Notification:
    event_type: str       # "step_started", "agent_response", "agent_notification", ...
    payload: dict
    step_id: str | None = None
    run_id: str | None = None
    target: str = "ui"    # "ui" | "log" | "debug"
    timestamp: float = field(default_factory=time.time)


# ── Subscriber protocol ───────────────────────────────────────────────

Subscriber = Callable[[Notification], Awaitable[None]]


# ── NotificationBus ──────────────────────────────────────────────────────

class NotificationBus:
    """Publish/subscribe bus for pipeline events.

    Default impl writes to outbox table. Host apps inject subscribers
    for real-time push (SSE, WebSocket, etc.).
    """

    def __init__(self, db_path: str = ":memory:"):
        self._subscribers: list[Subscriber] = []
        self._db_path = db_path
        self._conn = None  # lazy init from SkillFlow's connection
        # B3: asyncio only holds a weak ref to fire-and-forget tasks, so they get
        # GC'd while pending ("Task was destroyed but it is pending!"). Keep a
        # strong ref until each task completes.
        self._bg_tasks: set = set()

    # ── Subscriber management ──────────────────────────────────────

    def subscribe(self, callback: Subscriber) -> None:
        """Register a subscriber for real-time push."""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Subscriber) -> None:
        self._subscribers.remove(callback)

    # ── Publish ────────────────────────────────────────────────────

    async def publish(self, event_type: str, payload: dict,
                      step_id: str | None = None,
                      run_id: str | None = None,
                      target: str = "ui") -> None:
        """Publish an event to all subscribers AND outbox."""
        notification = Notification(
            event_type=event_type,
            payload=payload,
            step_id=step_id,
            run_id=run_id,
            target=target,
        )
        # Push to subscribers
        for sub in self._subscribers:
            try:
                await sub(notification)
            except Exception:
                pass  # subscriber errors must not break the pipeline

        # Write to outbox (persistent)
        self._write_outbox(notification)

    def publish_sync(self, event_type: str, payload: dict,
                     step_id: str | None = None,
                     run_id: str | None = None,
                     target: str = "ui") -> None:
        """Synchronous publish for non-async contexts."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — just write outbox
            notification = Notification(
                event_type=event_type, payload=payload,
                step_id=step_id, run_id=run_id, target=target,
            )
            self._write_outbox(notification)
            return
        task = loop.create_task(self.publish(event_type, payload,
                                             step_id=step_id, run_id=run_id,
                                             target=target))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ── Outbox ──────────────────────────────────────────────────────

    def set_connection(self, conn):
        """Share SkillFlow's SQLite connection for outbox writes."""
        self._conn = conn

    def _write_outbox(self, notification: Notification) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                """
                INSERT INTO skillflow_outbox (event_type, payload_json, stream_target, created_at)
                VALUES (?, ?, ?, datetime('now'))
                """,
                (
                    notification.event_type,
                    json.dumps({
                        "payload": notification.payload,
                        "step_id": notification.step_id,
                        "run_id": notification.run_id,
                        "timestamp": notification.timestamp,
                    }),
                    notification.target,
                ),
            )
            self._conn.commit()
        except Exception:
            pass  # outbox write must not fail the pipeline


# ── Filter helper ──────────────────────────────────────────────────────

def should_notify(notify_config: list[str] | None, event_type: str) -> bool:
    """Check if event_type matches the step's notify config.

    None or empty = notify nothing (only outbox). ["*"] = notify everything.
    """
    if notify_config is None or len(notify_config) == 0:
        return False
    if "*" in notify_config:
        return True
    return event_type in notify_config


# ── Event type constants ───────────────────────────────────────────────

STEP_STARTED = "step_started"
STEP_COMPLETED = "step_completed"
STEP_FAILED = "step_failed"
AGENT_RESPONSE = "agent_response"
AGENT_NOTIFICATION = "agent_notification"  # from notify tool
FILES_WRITTEN = "files_written"
CHECKPOINT_REACHED = "checkpoint_reached"
CHECKPOINT_REJECTED = "step_checkpoint_rejected"
RUN_STARTED = "run_started"
RUN_COMPLETED = "run_completed"
