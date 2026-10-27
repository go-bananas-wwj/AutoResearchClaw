"""Transport heartbeats must never enter the research conversation."""
import json


def is_heartbeat(raw: str) -> bool:
    if raw.strip() == "ping":  # Older dashboard clients send bare text.
        return True
    try:
        message = json.loads(raw)
    except ValueError:
        return False
    return (isinstance(message, dict) and message.get("type") in ("ping", "heartbeat")
            and "text" not in message and "message" not in message)
