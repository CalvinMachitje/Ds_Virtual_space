# services/auth-service/app/routes/admin.py
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from typing import Optional

from app.core.config import settings
from app.dependencies.rate_limiter import limiter
from app.services.supabase_service import supabase
from app.utils.audit import log_action
from app.utils.event_bus import publish_event
from .twofa import verify_2fa_code
from utils.redis_utils import safe_redis_call
from utils.utils import generate_tokens

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str
    otp: Optional[str] = None


@router.post("/login")
@limiter.limit(settings.RATE_LIMIT_ADMIN_LOGIN)
async def admin_login(data: AdminLoginRequest, request: Request):
    """
    Admin login endpoint with:
    - Redis-based lock + fail counter
    - Lockout after threshold
    - Admin table check
    - Optional OTP (2FA)
    - Admin-specific JWT claims
    """
    email = data.email.strip().lower()
    password = data.password
    otp = data.otp
    ip = request.client.host or "unknown"

    fail_key = f"admin_fail:{email}:{ip}"
    lock_key = f"admin_lock:{email}:{ip}"

    # Check existing lock
    if safe_redis_call("exists", lock_key):
        ttl = safe_redis_call("ttl", lock_key, default=0)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Account locked — try again in {ttl // 60 + 1} minutes"
        )

    # Acquire short lock to prevent concurrent attempts
    acquired = safe_redis_call("set", lock_key, "1", nx=True, ex=10)
    if not acquired:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="Concurrent login attempt — wait")

    try:
        # Attempt Supabase password login
        auth_resp = supabase.auth.sign_in_with_password({"email": email, "password": password})
        user = auth_resp.user

        if not user:
            fails = safe_redis_call("incr", fail_key) or 1
            safe_redis_call("expire", fail_key, 1800)
            if fails >= settings.ADMIN_FAIL_THRESHOLD:
                safe_redis_call("setex", lock_key, settings.ADMIN_LOCKOUT_MINUTES * 60, "locked")
                log_action(None, "admin_account_locked", {"email": email, "ip": ip, "fails": fails})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

        # Verify admin record exists
        admin_res = supabase.table("admins").select("*").eq("id", user.id).maybe_single().execute()
        admin = admin_res.data

        if not admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not registered as admin")

        if not user.email_confirmed_at:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="Email not confirmed",
                headers={"X-Needs-Confirmation": "true"}
            )

        # Optional 2FA check (if enabled on admin profile)
        profile_res = supabase.table("profiles").select("two_factor_enabled").eq("id", user.id).maybe_single().execute()
        profile = profile_res.data or {}
        if profile.get("two_factor_enabled") and not otp:
            publish_event("auth.events", {"event": "2fa_required", "user_id": user.id, "email": email})
            raise HTTPException(
                status.HTTP_200_OK,
                detail="2FA code required",
                headers={"X-2FA-Required": "true"}
            )

        if profile.get("two_factor_enabled") and otp:
            # Reuse your existing verify function
            if not verify_2fa_code(user.id, otp):  # from .twofa
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid 2FA code")
            publish_event("auth.events", {"event": "2fa_verified", "user_id": user.id})

        # Success — generate tokens with admin claims
        claims = {
            "role": "admin",
            "admin_level": admin.get("admin_level", 1)
        }
        access_token, refresh_token = generate_tokens(str(user.id), additional_claims=claims)

        log_action(
            user.id,
            "admin_login_success",
            {"email": email, "admin_level": admin["admin_level"], "ip": ip}
        )
        publish_event("auth.events", {
            "event": "admin_login",
            "admin_id": user.id,
            "email": email,
            "admin_level": admin["admin_level"],
            "ip": ip
        })

        # Cleanup
        safe_redis_call("delete", fail_key)
        safe_redis_call("delete", lock_key)

        return {
            "success": True,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user": {**admin, "email": user.email, "role": "admin"}
        }

    except HTTPException:
        raise
    except Exception as e:
        safe_redis_call("incr", fail_key)
        safe_redis_call("expire", fail_key, 1800)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication failed")
    finally:
        safe_redis_call("delete", lock_key)