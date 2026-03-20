# services/auth-service/app/utils/utils.py
import re
from datetime import datetime, timedelta, timezone
from jose import jwt
from app.core.config import settings

from app.utils.audit import log_action
from utils.redis_utils import safe_redis_call



def is_strong_password(password: str) -> tuple[bool, str]:
    if len(password) < 12:
        return False, "Password must be at least 12 characters long"
    if not re.search(r"[A-Z]", password):
        return False, "Must contain at least one uppercase letter"
    if not re.search(r"[a-z]", password):
        return False, "Must contain at least one lowercase letter"
    if not re.search(r"[0-9]", password):
        return False, "Must contain at least one number"
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        return False, "Must contain at least one special character"
    return True, ""


def generate_tokens(user_id: str, additional_claims: dict | None = None) -> tuple[str, str]:
    claims = additional_claims or {}
    access_token = jwt.encode(
        {
            "sub": user_id,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRES_MINUTES),
            **claims
        },
        settings.JWT_SECRET_KEY,
        algorithm="HS256"
    )
    refresh_token = jwt.encode(
        {
            "sub": user_id,
            "exp": datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRES_DAYS),
            "type": "refresh"
        },
        settings.JWT_SECRET_KEY,
        algorithm="HS256"
    )
    return access_token, refresh_token


def blacklist_jwt(token: str):
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=["HS256"])
        jti = payload.get("jti")
        if not jti:
            return
        exp = payload.get("exp", 0)
        ttl = max(3600, exp - int(datetime.now(timezone.utc).timestamp()))
        safe_redis_call("setex", f"blacklist:{jti}", ttl, "true")
    except Exception:
        pass