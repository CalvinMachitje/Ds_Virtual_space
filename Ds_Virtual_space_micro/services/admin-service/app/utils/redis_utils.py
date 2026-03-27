# Redis utility functions for the Admin Service, providing a safe way to interact with Redis without crashing the app if Redis is down.
# services/admin-service/app/utils/redis_utils.py
import redis
from functools import wraps
from app.core.config import settings

redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)

def safe_redis_call(method: str, *args, default=None, **kwargs):
    """Safe Redis call that won't crash the app if Redis is down"""
    try:
        if not redis_client:
            return default
        func = getattr(redis_client, method)
        return func(*args, **kwargs)
    except Exception:
        return default