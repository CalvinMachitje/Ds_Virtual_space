# Rate limiter setup for the Admin Service using SlowAPI.
# services/admin-service/app/dependencies/rate_limiter.py
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.middleware import SlowAPIMiddleware
from app.core.config import settings

limiter = Limiter(key_func=get_remote_address, default_limits=[settings.RATE_LIMIT_GENERAL])