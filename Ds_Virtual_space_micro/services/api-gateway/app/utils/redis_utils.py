import redis
from functools import wraps
from app.core.config import settings

redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)

def safe_redis_call(method: str, *args, default=None, **kwargs):
    """Safe Redis wrapper - won't crash if Redis is down"""
    try:
        if not redis_client:
            return default
        func = getattr(redis_client, method)
        return func(*args, **kwargs)
    except Exception:
        return default