# Event bus utility for the Admin Service, allowing events to be published to Redis pub/sub.
# services/admin-service/app/utils/event_bus.py
import json
from app.utils.redis_utils import redis_client

def publish_event(channel: str, message: dict):
    """Publish event to Redis pub/sub"""
    try:
        if redis_client:
            redis_client.publish(channel, json.dumps(message))
    except Exception:
        pass  # graceful degradation