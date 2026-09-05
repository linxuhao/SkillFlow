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
import logging
import threading
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
        # Main event loop reference for thread-safe publish from executor threads.
        self._main_loop: asyncio.AbstractEventLoop | None = None
        # Lock protecting _write_outbox access to self._conn, so a worker-thread
        # fallback (no main loop) doesn't conflict with SkillFlow._tx() blocks.
        self._conn_lock = threading.RLock()

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Store the main event loop for cross-thread publish support."""
        self._main_loop = loop

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
        """Synchronous publish — safe from any thread.

        From the event loop thread: schedules publish as async task.
        From a worker thread (e.g. thread-pool executor): bridges to the
        main event loop via call_soon_threadsafe so subscribers (SSE) fire
        and the outbox is written.
        """
        try:
            loop = asyncio.get_running_loop()
            # On the event loop thread — schedule as async task
            task = loop.create_task(self.publish(event_type, payload,
                                                 step_id=step_id, run_id=run_id,
                                                 target=target))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            # Worker thread (no running loop) — bridge to main loop.
            # If no main loop is set, fall back to synchronous outbox-only write.
            if self._main_loop and self._main_loop.is_running():
                self._main_loop.call_soon_threadsafe(
                    lambda: self._schedule_publish(event_type, payload,
                                                   step_id, run_id, target)
                )
            else:
                notification = Notification(
                    event_type=event_type, payload=payload,
                    step_id=step_id, run_id=run_id, target=target,
                )
                # `_write_outbox` takes the lock itself now, for every caller
                # rather than only this one — the loop-thread path through
                # `publish()` was the one that had none.
                self._write_outbox(notification)

    def _schedule_publish(self, event_type: str, payload: dict,
                          step_id: str | None, run_id: str | None,
                          target: str) -> None:
        """Schedule publish on the event loop (called via call_soon_threadsafe)."""
        task = asyncio.ensure_future(self.publish(
            event_type, payload, step_id=step_id, run_id=run_id, target=target))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ── Outbox ──────────────────────────────────────────────────────

    def set_connection(self, conn, lock=None):
        """Share SkillFlow's SQLite connection — and the lock that guards it.

        Sharing the connection without the lock is what made the outbox write
        unsafe: `SkillFlow._tx()` serialises transactions on `SkillFlow._lock`,
        this bus serialised (some of) its writes on its own `_conn_lock`, and
        two locks over one connection serialise nothing. Callers that pass only
        a connection keep the old private lock, so an embedder that never shared
        one is unaffected.
        """
        self._conn = conn
        if lock is not None:
            self._conn_lock = lock

    def _write_outbox(self, notification: Notification) -> None:
        """Persist one event. Serialised against SkillFlow's transactions.

        THE LOCK IS THE FIX. `publish()` runs on the event loop, and the host
        runs `advance_run` in a worker thread (AItelier bridges the publish back
        with `call_soon_threadsafe`), so this INSERT executed on the loop thread
        while a worker thread sat inside `SkillFlow._tx()` with `BEGIN IMMEDIATE`
        open — same connection, no shared lock. Two things went wrong and both
        were silent:

        * the INSERT joined the worker's open transaction and `commit()` ended
          it early, from the wrong thread;
        * sqlite then raised `cannot start a transaction within a transaction`
          on the next `BEGIN IMMEDIATE`, the bare `except` below swallowed it,
          and the event vanished.

        Live, 2026-09-05: two `coding_impl` runs reached `completed` with no
        `run_completed` anywhere, so `debugctl await --follow` — the documented
        way to wait for a run — waited out its full timeout on a finished run.

        Holding this lock can block the event loop for the length of a
        transaction. That is bounded and it is the point: `_tx` blocks are SQL
        only (tools and hooks run outside them), and a short wait is the correct
        price for not corrupting the connection everything else reads.
        """
        if self._conn is None:
            return
        try:
            with self._conn_lock:
                self._conn.execute(
                    """
                    INSERT INTO skillflow_outbox (event_type, payload_json, stream_target, created_at)
                    VALUES (?, ?, ?, datetime('now'))
                    """,
                    (
                        notification.event_type,
                        json.dumps({
                            **notification.payload,
                            "_step_id": notification.step_id,
                            "_run_id": notification.run_id,
                            "_timestamp": notification.timestamp,
                        }),
                        notification.target,
                    ),
                )
                self._conn.commit()
        except Exception as e:                                   # noqa: BLE001
            # Still must not fail the pipeline — but never silently again. A
            # dropped terminal event is indistinguishable from a run that has
            # not finished, and that is exactly how this hid.
            logging.getLogger("skillflow.notifications").warning(
                "outbox write dropped for %r (run=%s step=%s): %s: %s",
                notification.event_type, notification.run_id,
                notification.step_id, type(e).__name__, e)


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
