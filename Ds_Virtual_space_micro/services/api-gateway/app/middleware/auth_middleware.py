# services/api-gateway/app/middleware/auth_middleware.py
from fastapi import HTTPException, status
from jose import jwt, JWTError
import httpx
import os

from app.core.config import settings
from app.utils.redis_utils import safe_redis_call

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY") or settings.JWT_SECRET_KEY
JWT_ALGORITHM = "HS256"


async def verify_jwt(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        jti = payload.get("jti")
        if jti and safe_redis_call("get", f"blacklist:{jti}") == "true":
            return None
        return payload
    except JWTError:
        return None
    except Exception:
        return None


async def get_user_from_auth_service(token: str):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "http://localhost:5001/api/auth/verify-token",
                headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return None


async def get_current_user(token: str):
    """Main dependency - called from gateway"""
    payload = await verify_jwt(token)
    if not payload:
        payload = await get_user_from_auth_service(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

    return {
        "user_id": payload.get("sub") or payload.get("user_id"),
        "role": payload.get("role"),
        "admin_level": payload.get("admin_level")
    }


async def get_current_admin(token: str):
    user = await get_current_user(token)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user