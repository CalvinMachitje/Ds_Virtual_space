import os
import redis
import json
import logging

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def publish_event(channel: str, event: dict):
    """
    Publish a JSON event to a Redis channel.
    """
    try:
        redis_client.publish(channel, json.dumps(event))
        logger.debug(f"Published event to {channel}: {event}")
    except Exception as e:
        logger.error(f"Failed to publish event to {channel}: {str(e)}")


def subscribe(channel: str):
    """
    Returns a Redis PubSub object subscribed to the given channel.
    """
    pubsub = redis_client.pubsub()
    pubsub.subscribe(channel)
    logger.info(f"Subscribed to Redis channel: {channel}")
    return pubsub