# services/auth-service/app/utils/event_bus.py
import json
import logging
from app.utils.redis_utils import redis_client


logger = logging.getLogger(__name__)


def publish_event(channel: str, event: dict):
    try:
        if redis_client:
            redis_client.publish(channel, json.dumps(event))
            logger.debug(f"Published to {channel}: {event}")
    except Exception as e:
        logger.error(f"Event publish failed: {e}")