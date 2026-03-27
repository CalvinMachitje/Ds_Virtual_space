# services/auth-service/app/routes/auth.py
from fastapi import APIRouter, Depends, HTTPException, Request, status
import jwt
from pydantic import BaseModel, EmailStr
from typing import Optional

from app.core.config import settings
from app.dependencies.auth import get_current_user, oauth2_scheme
from app.dependencies.rate_limiter import limiter
from app.services.supabase_service import supabase
from app.utils.audit import log_action
from app.utils.event_bus import publish_event
from app.utils.redis_utils import safe_redis_call
from app.utils.extensions import blacklist_jwt, generate_tokens, is_strong_password

from .twofa import verify_2fa_code

router = APIRouter( tags=["auth"])


class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    phone: Optional[str] = None
    role: str  # "buyer" | "seller"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    otp: Optional[str] = None


class RefreshRequest(BaseModel):
    refresh_token: str


@router.get("/ping")
async def ping():
    return {"pong": True}


@router.post("/signup", status_code=status.HTTP_201_CREATED)
@limiter.limit(settings.RATE_LIMIT_SIGNUP)
async def signup(data: SignupRequest, request: Request):
    email = data.email.strip().lower()
    ip = request.client.host or "unknown"

    if data.role not in ["buyer", "seller"]:
        raise HTTPException(400, detail=f"Role must be one of ['buyer', 'seller']")

    valid, msg = is_strong_password(data.password)
    if not valid:
        raise HTTPException(400, detail=msg)

    lock_key = f"signup_lock:{email}:{ip}"

    if safe_redis_call("exists", lock_key):
        ttl = safe_redis_call("ttl", lock_key, default=0)
        raise HTTPException(429, detail=f"Too many signup attempts — try again in {ttl//60 + 1} min")

    acquired = safe_redis_call("set", lock_key, "1", nx=True, ex=10)
    if not acquired:
        ttl = safe_redis_call("ttl", lock_key, default=0)
        raise HTTPException(429, detail=f"Concurrent signup attempt — wait")

    try:
        existing = supabase.table("profiles").select("id").eq("email", email).maybe_single().execute()
        if existing.data:
            raise HTTPException(409, detail="Email already registered")

        sign_up = supabase.auth.sign_up({
            "email": email,
            "password": data.password,
            "options": {"data": {"full_name": data.full_name, "phone": data.phone or "", "role": data.role}}
        })

        if not sign_up.user:
            raise HTTPException(500, detail="User creation failed")

        supabase.table("profiles").insert({
            "id": sign_up.user.id,
            "full_name": data.full_name,
            "email": email,
            "phone": data.phone,
            "role": data.role,
            "created_at": "now()",
            "updated_at": "now()"
        }).execute()

        log_action(sign_up.user.id, "signup", {"email": email, "role": data.role}, ip=ip)
        publish_event("auth.events", {
            "event": "user_registered",
            "user_id": sign_up.user.id,
            "email": email,
            "full_name": data.full_name,
            "role": data.role
        })

        if not sign_up.session:
            return {"success": True, "message": "Check email to confirm", "email_confirmation_sent": True}

        access, refresh = generate_tokens(str(sign_up.user.id))
        return {
            "success": True,
            "access_token": access,
            "refresh_token": refresh,
            "user": {
                "id": sign_up.user.id,
                "email": email,
                "full_name": data.full_name,
                "role": data.role,
                "phone": data.phone
            }
        }

    finally:
        safe_redis_call("delete", lock_key)


@router.post("/login")
@limiter.limit(settings.RATE_LIMIT_LOGIN)
async def login(data: LoginRequest, request: Request):
    email = data.email.strip().lower()
    password = data.password
    otp = data.otp
    ip = request.client.host or "unknown"

    fail_key = f"login_fail:{email}:{ip}"
    lock_key = f"login_lock:{email}:{ip}"

    if safe_redis_call("exists", lock_key):
        ttl = safe_redis_call("ttl", lock_key, default=0)
        raise HTTPException(429, detail=f"Locked — try again in {ttl//60 + 1} min")

    acquired = safe_redis_call("set", lock_key, "1", nx=True, ex=10)
    if not acquired:
        raise HTTPException(429, detail="Concurrent attempt — wait")

    try:
        auth_resp = supabase.auth.sign_in_with_password({"email": email, "password": password})
        user = auth_resp.user

        if not user:
            fails = safe_redis_call("incr", fail_key) or 1
            safe_redis_call("expire", fail_key, 1800)
            raise HTTPException(401, detail="Invalid credentials")

        profile = supabase.table("profiles").select("*").eq("id", user.id).maybe_single().execute().data or {}

        if profile.get("banned"):
            raise HTTPException(403, detail="Account banned")

        if not user.email_confirmed_at:
            raise HTTPException(403, detail="Email not confirmed", headers={"X-Needs-Confirmation": "true"})

        # 2FA check
        if profile.get("two_factor_enabled"):
            if not otp:
                publish_event("auth.events", {"event": "2fa_required", "user_id": user.id, "email": email})
                return {"requires_2fa": True, "message": "2FA code required"}
            if not verify_2fa_code(user.id, otp):
                raise HTTPException(401, detail="Invalid 2FA code")
            publish_event("auth.events", {"event": "2fa_verified", "user_id": user.id, "email": email})

        safe_redis_call("delete", fail_key)
        safe_redis_call("delete", lock_key)

        access, refresh = generate_tokens(str(user.id))
        log_action(user.id, "user_login", {"email": email, "role": profile.get("role", "unknown")}, ip=ip)
        publish_event("auth.events", {"event": "user_logged_in", "user_id": user.id, "email": email, "role": profile.get("role")})

        return {
            "success": True,
            "access_token": access,
            "refresh_token": refresh,
            "user": {**profile, "email": user.email, "email_confirmed": bool(user.email_confirmed_at)}
        }

    except Exception as e:
        safe_redis_call("incr", fail_key)
        safe_redis_call("expire", fail_key, 1800)
        raise HTTPException(401, detail="Login failed")
    finally:
        safe_redis_call("delete", lock_key)


@router.post("/refresh")
async def refresh_token(data: RefreshRequest):
    try:
        payload = jwt.decode(data.refresh_token, settings.JWT_SECRET_KEY, algorithms=["HS256"])

        if payload.get("type") != "refresh":
            raise HTTPException(401, detail="Invalid token type")

        user_id = payload.get("sub")
        jti = payload.get("jti")

        # blacklist old refresh token (rotation)
        safe_redis_call("setex", f"blacklist:{jti}", 86400, "true")

        access_token, new_refresh_token = generate_tokens(user_id)

        publish_event("auth.events", {
            "event": "token_refreshed",
            "user_id": user_id
        })

        return {
            "access_token": access_token,
            "refresh_token": new_refresh_token
        }

    except Exception:
        raise HTTPException(401, detail="Invalid or expired refresh token")


@router.get("/me")
async def get_current_user_info(current_user: str = Depends(get_current_user)):
    profile = supabase.table("profiles").select("*").eq("id", current_user).maybe_single().execute().data
    if not profile:
        raise HTTPException(404, detail="Profile not found")
    return {"user": profile}


@router.post("/logout")
async def logout(token: str = Depends(oauth2_scheme)):
    blacklist_jwt(token)
    return {"success": True, "message": "Logged out"}


@router.post("/verify-email")
async def verify_email(token: str):
    try:
        verified = supabase.auth.verify_otp({"token_hash": token, "type": "signup"})
        if not verified.user:
            raise HTTPException(400, detail="Invalid or expired token")

        user_id = verified.user.id
        supabase.table("profiles").update({"email_verified": True, "updated_at": "now()"}).eq("id", user_id).execute()

        access, refresh = generate_tokens(user_id)
        log_action(user_id, "email_verified")
        publish_event("auth.events", {"event": "email_verified", "user_id": user_id})

        return {
            "success": True,
            "message": "Email verified",
            "access_token": access,
            "refresh_token": refresh
        }
    except Exception as e:
        raise HTTPException(400, detail=str(e))