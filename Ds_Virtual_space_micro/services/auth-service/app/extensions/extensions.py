# services/auth-service/app/extensions.py
from datetime import time
import os
import redis
import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)

redis_client: Optional[redis.Redis] = None

def init_redis(app):
    global redis_client
    if redis_client is not None:
        logger.debug("Redis already initialized")
        return

    redis_url = app.config.get("REDIS_URL") or os.getenv("REDIS_URL") or "redis://localhost:6379/0"
    max_retries, retry_delay = 5, 2

    for attempt in range(max_retries):
        try:
            redis_client = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5
            )
            if redis_client.ping():
                logger.info(f"Redis connected successfully: {redis_url}")
                return
        except Exception as e:
            logger.warning(f"Redis connection attempt {attempt+1} failed: {e}")
            time.sleep(retry_delay)

    logger.critical(f"Redis connection FAILED after {max_retries} attempts")
    redis_client = None

def safe_redis_call(method_name: str, *args, default: Any = None) -> Any:
    global redis_client
    if redis_client is None:
        return default
    try:
        method = getattr(redis_client, method_name)
        return method(*args)
    except Exception:
        return default