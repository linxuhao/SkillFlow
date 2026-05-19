"""Tests for stepflow.notifications — NotificationBus + should_notify."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from stepflow.notifications import (
    Notification, NotificationBus, should_notify,
    STEP_STARTED, STEP_COMPLETED, AGENT_NOTIFICATION,
)


class TestShouldNotify:
    def test_none_config_returns_false(self):
        assert should_notify(None, STEP_STARTED) is False

    def test_empty_config_returns_false(self):
        assert should_notify([], STEP_STARTED) is False

    def test_star_matches_all(self):
        assert should_notify(["*"], "anything") is True

    def test_exact_match(self):
        assert should_notify(["step_started", "step_completed"], STEP_STARTED) is True

    def test_no_match(self):
        assert should_notify(["step_completed"], STEP_STARTED) is False


class TestNotificationBus:
    @pytest.fixture
    def bus(self):
        return NotificationBus(":memory:")

    def test_subscribe_and_publish(self, bus):
        received = []
        async def handler(n: Notification):
            received.append(n)

        bus.subscribe(handler)
        import asyncio
        asyncio.run(bus.publish("test_event", {"key": "val"},
                                step_id="s1", run_id="r1"))

        assert len(received) == 1
        assert received[0].event_type == "test_event"
        assert received[0].payload == {"key": "val"}

    def test_publish_sync_no_loop(self, bus):
        """publish_sync works when no event loop is running."""
        # Should not raise
        bus.publish_sync("test", {"x": 1}, step_id="s1")

    def test_unsubscribe_stops_receiving(self, bus):
        received = []
        async def handler(n: Notification):
            received.append(n)

        bus.subscribe(handler)
        bus.unsubscribe(handler)
        import asyncio
        asyncio.run(bus.publish("test", {}))

        assert len(received) == 0

    def test_subscriber_exception_does_not_break_publish(self, bus):
        async def bad_handler(n: Notification):
            raise RuntimeError("boom")

        received = []
        async def good_handler(n: Notification):
            received.append(n)

        bus.subscribe(bad_handler)
        bus.subscribe(good_handler)
        import asyncio
        asyncio.run(bus.publish("test", {}))

        assert len(received) == 1  # good handler still got it

    def test_notification_dataclass(self):
        n = Notification(
            event_type="step_started",
            payload={"step": "1_5"},
            step_id="1_5",
            run_id="abc",
            target="ui",
        )
        assert n.event_type == "step_started"
        assert n.target == "ui"
        assert n.timestamp > 0


class TestNotifyToolAutoContext:
    def test_notify_accepts_context_kwargs(self):
        """notify tool auto-receives context from stepflow."""
        from stepflow.tools.notify.impl import notify

        result = notify(
            "hello world", "milestone",
            run_id="run-1", step_id="1_5",
            config_name="dpe_default", step_name="researcher",
            step_type="agent",
        )
        assert result["notified"] is True
        assert result["level"] == "milestone"

    def test_notify_truncates_long_message(self):
        from stepflow.tools.notify.impl import notify

        result = notify("x" * 1000, "info")
        assert result["message_length"] == 500

    def test_notify_default_level(self):
        from stepflow.tools.notify.impl import notify

        result = notify("test")
        assert result["level"] == "info"

    def test_notify_with_emit_callback(self):
        from stepflow.tools.notify.impl import notify
        from unittest.mock import MagicMock
        import asyncio

        called = []
        async def cb(event_type, payload):
            called.append((event_type, payload))

        # Need a running loop for emit_callback
        async def run():
            notify("test", emit_callback=cb, run_id="r1",
                   step_id="s1", config_name="cfg", step_name="n",
                   step_type="agent")

        asyncio.run(run())
        assert len(called) == 1
        assert called[0][0] == "agent_notification"
        assert called[0][1]["message"] == "test"
        assert called[0][1]["run_id"] == "r1"
        assert called[0][1]["step_id"] == "s1"
        assert called[0][1]["config_name"] == "cfg"


class TestNotificationBusEdgeCases:
    def test_publish_sync_with_running_loop(self):
        """publish_sync when loop is running schedules a task."""
        import asyncio
        bus = NotificationBus(":memory:")
        received = []
        async def handler(n):
            received.append(n)
        bus.subscribe(handler)

        async def test():
            bus.publish_sync("e1", {"x": 1}, step_id="s1")
            await asyncio.sleep(0.01)  # let task run

        asyncio.run(test())
        assert len(received) == 1

    def test_write_outbox_no_connection_no_crash(self):
        """_write_outbox does not crash when connection is None."""
        bus = NotificationBus(":memory:")
        # _conn is None by default
        bus._write_outbox(Notification("test", {}))
        # Should not raise

    def test_notification_all_fields(self):
        n = Notification(
            event_type="agent_notification",
            payload={"msg": "hello"},
            step_id="s1",
            run_id="r1",
            target="debug",
            timestamp=1000.0,
        )
        assert n.event_type == "agent_notification"
        assert n.step_id == "s1"
        assert n.run_id == "r1"
        assert n.target == "debug"
        assert n.timestamp == 1000.0
