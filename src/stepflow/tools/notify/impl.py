"""Send a user-visible notification with auto-injected context.

Stepflow enriches every notification with:
  - run_id, step_id, config_name (pipeline name)
  - step_name (agent_config or tool_name), step_type
  - timestamp

The agent only provides message + level. Everything else is automatic.
"""

import time


def notify(message: str, level: str = "info", *,
           # Auto-injected by stepflow (agents don't pass these)
           workspace_root: str = "",
           emit_callback=None,
           run_id: str = "",
           step_id: str = "",
           config_name: str = "",
           step_name: str = "",
           step_type: str = "agent",
           ) -> dict:
    """Publish a context-enriched notification."""
    message = message[:500]

    notification = {
        "message": message,
        "level": level,
        "run_id": run_id,
        "step_id": step_id,
        "config_name": config_name,
        "step_name": step_name,
        "step_type": step_type,
        "timestamp": time.time(),
    }

    if emit_callback is not None:
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            loop.create_task(
                emit_callback("agent_notification", notification)
            )
        except RuntimeError:
            pass

    return {
        "notified": True,
        "level": level,
        "message_length": len(message),
    }
