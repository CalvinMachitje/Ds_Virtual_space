# services/auth-service/app/utils/redis_utils.py
import os
import logging
from typing import Any, Optional

import redis

logger = logging.getLogger(__name__)

redis_client: Optional[redis.Redis] = None


def init_redis():
    global redis_client
    if redis_client is not None:
        return

    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    try:
        redis_client = redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            retry_on_timeout=True,
            max_connections=20
        )
        redis_client.ping()
        logger.info(f"Redis connected: {url}")
    except Exception as e:
        logger.error(f"Redis connection failed: {e}")
        redis_client = None


def safe_redis_call(method_name: str, *args, default: Any = None) -> Any:
    if redis_client is None:
        logger.warning(f"Redis unavailable - {method_name} skipped")
        return default
    try:
        method = getattr(redis_client, method_name)
        return method(*args)
    except Exception as e:
        logger.error(f"Redis call failed ({method_name}): {e}")
        return default