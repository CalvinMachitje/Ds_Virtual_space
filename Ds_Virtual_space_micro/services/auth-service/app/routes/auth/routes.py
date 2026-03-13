# services/auth-service/app/routes/auth/routes.py
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from flask_cors import cross_origin
import logging

import httpx

from app.services.supabase_service import supabase
from app.utils.audit import log_action
from app.extensions import limiter, safe_redis_call
from app.constants import USER_FAIL_THRESHOLD, USER_LOCKOUT_MINUTES, RATE_LIMIT_SIGNUP, ROLES
from app.utils.utils import blacklist_jwt, generate_tokens, handle_login_fail, is_strong_password
from app.utils.event_bus import publish_event  # Redis Pub/Sub events

# Optional 2FA import
from .twofa import verify_2fa_code

bp = Blueprint("auth_routes_bp", __name__, url_prefix="/")
logger = logging.getLogger(__name__)

# ──────────────────────────
# GET /ping
# ──────────────────────────
@bp.route("/ping")
def ping():
    logger.info("Ping endpoint called")
    current_app.logger.debug("Debug log inside route")
    return jsonify({"pong": True})


# ──────────────────────────
# POST /signup with Redis lock
# ──────────────────────────
@bp.route("/signup", methods=["POST"])
@limiter.limit(RATE_LIMIT_SIGNUP)
@cross_origin(origins=["http://localhost:5173", "*"])
def signup():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    full_name = (data.get("full_name") or "").strip()
    phone = (data.get("phone") or "").strip()
    role = (data.get("role") or "").strip().lower()

    if not all([email, password, full_name, role]):
        return jsonify({"error": "Missing required fields"}), 400
    if role not in ROLES:
        return jsonify({"error": f"Role must be one of {ROLES}"}), 400

    is_valid, msg = is_strong_password(password)
    if not is_valid:
        return jsonify({"error": msg}), 400

    ip = request.remote_addr or "unknown"
    lock_key = f"signup_lock:{email}:{ip}"
    # Check if lock exists
    if safe_redis_call("exists", lock_key):
        ttl = safe_redis_call("ttl", lock_key, default=0)
        return jsonify({"error": f"Too many signup attempts. Try again in {ttl // 60 + 1} min"}), 429

    # Acquire lock: use setnx + expire
    acquired = safe_redis_call("setnx", lock_key, "1")
    if not acquired:
        ttl = safe_redis_call("ttl", lock_key, default=0)
        return jsonify({"error": f"Too many signup attempts. Try again in {ttl // 60 + 1} min"}), 429
    safe_redis_call("expire", lock_key, 10)  # 10 seconds short lock to prevent concurrent attempts

    try:
        # Check if email already exists
        existing = supabase.table("profiles").select("id").eq("email", email).maybe_single().execute()
        if existing.data:
            return jsonify({"error": "Email already registered"}), 409

        sign_up = supabase.auth.sign_up({
            "email": email,
            "password": password,
            "options": {"data": {"full_name": full_name, "phone": phone, "role": role}}
        })
        user = sign_up.get("user")
        if not user:
            return jsonify({"error": "User creation failed"}), 500

        supabase.table("profiles").insert({
            "id": user["id"],
            "full_name": full_name,
            "email": email,
            "phone": phone,
            "role": role,
            "created_at": "now()",
            "updated_at": "now()"
        }).execute()

        log_action(user["id"], "signup", {"email": email, "role": role})
        publish_event("auth.events", {
            "event": "user_registered",
            "user_id": user["id"],
            "email": email,
            "full_name": full_name,
            "role": role
        })

        # If email confirmation required
        if not sign_up.get("session"):
            return jsonify({"success": True, "message": "Check email to confirm", "email_confirmation_sent": True}), 200

        access, refresh = generate_tokens(str(user["id"]))
        return jsonify({
            "success": True,
            "access_token": access,
            "refresh_token": refresh,
            "user": {"id": user["id"], "email": email, "full_name": full_name, "role": role, "phone": phone}
        }), 201

    finally:
        safe_redis_call("delete", lock_key)

# ──────────────────────────
# POST /login with Redis lock
# ──────────────────────────
@bp.route("/login", methods=["POST"])
@cross_origin(origins=["http://localhost:5173", "*"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    otp = data.get("otp")
    ip = request.remote_addr or "unknown"

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    lock_key = f"login_lock:{email}:{ip}"
    fail_key = f"login_fail:{email}:{ip}"

    # Redis lock check
    if safe_redis_call("exists", lock_key):
        ttl = safe_redis_call("ttl", lock_key, default=0)
        return jsonify({"error": f"Too many failed attempts. Try again in {ttl // 60 + 1} min"}), 429

    # Acquire lock to prevent concurrent requests
    acquired = safe_redis_call("setnx", lock_key, "1")
    if not acquired:
        ttl = safe_redis_call("ttl", lock_key, default=0)
        return jsonify({"error": f"Too many login attempts. Try again in {ttl // 60 + 1} min"}), 429
    safe_redis_call("expire", lock_key, 10)  # short lock

    try:
        auth_resp = supabase.auth.sign_in_with_password({"email": email, "password": password})
        user = auth_resp.get("user")
        if not user:
            # Increment fail counter
            fails = safe_redis_call("incr", fail_key, default=0) or 1
            safe_redis_call("expire", fail_key, 1800)
            return jsonify({"error": "Invalid email or password"}), 401

        profile_res = supabase.table("profiles").select("*").eq("id", user["id"]).maybe_single().execute()
        profile = profile_res.data or {}

        if profile.get("banned"):
            return jsonify({"error": "Account banned"}), 403
        if not user.get("email_confirmed_at"):
            return jsonify({"error": "Confirm email first", "needs_confirmation": True}), 403

        # 2FA handling
        if profile.get("two_factor_enabled") and not otp:
            publish_event("auth.events", {"event": "2fa_required", "user_id": user["id"], "email": email})
            return jsonify({"success": True, "requires_2fa": True, "message": "2FA code required"}), 200

        if profile.get("two_factor_enabled") and otp:
            verified = verify_2fa_code(user["id"], otp)
            if not verified:
                return jsonify({"error": "Invalid 2FA code"}), 401
            publish_event("auth.events", {"event": "2fa_verified", "user_id": user["id"], "email": email})

        # Clear fail counters
        safe_redis_call("delete", fail_key)
        safe_redis_call("delete", lock_key)

        access, refresh = generate_tokens(str(user["id"]))
        log_action(user["id"], "user_login", {"email": email, "role": profile.get("role", "unknown")})

        publish_event("auth.events", {"event": "user_logged_in", "user_id": user["id"], "email": email, "role": profile.get("role")})

        return jsonify({
            "success": True,
            "access_token": access,
            "refresh_token": refresh,
            "user": {**profile, "email": user["email"], "email_confirmed": bool(user.get("email_confirmed_at"))}
        }), 200

    except Exception as e:
        logger.exception(f"Login failed {email}")
        # Increment fail counter even on unexpected errors
        safe_redis_call("incr", fail_key, default=0)
        safe_redis_call("expire", fail_key, 1800)
        return jsonify({"error": "Login failed. Try again."}), 401

    finally:
        safe_redis_call("delete", lock_key)

# ──────────────────────────
# POST /refresh
# ──────────────────────────
@bp.route("/refresh", methods=["POST"])
def refresh():
    data = request.get_json(silent=True) or {}
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        return jsonify({"error": "Refresh token required"}), 401
    try:
        from flask_jwt_extended import decode_token
        decoded = decode_token(refresh_token)
        user_id = decoded.get("sub")
        if not user_id:
            raise ValueError("Missing user ID in refresh token")
        new_access = generate_tokens(user_id)[0]

        publish_event("auth.events", {"event": "access_token_refreshed", "user_id": user_id})
        return jsonify({"access_token": new_access}), 200
    except Exception as e:
        logger.error(f"Refresh failed: {str(e)}")
        return jsonify({"error": "Invalid refresh token"}), 401


# ──────────────────────────
# GET /me
# ──────────────────────────
@bp.route("/me", methods=["GET"])
@jwt_required()
def me():
    user_id = get_jwt_identity()
    try:
        profile = supabase.table("profiles").select("*").eq("id", user_id).maybe_single().execute().data
        if not profile:
            return jsonify({"error": "Profile not found"}), 404
        return jsonify({"user": profile}), 200
    except Exception as e:
        logger.error(f"/me failed: {str(e)}")
        return jsonify({"error": "Failed to fetch user"}), 500


# ──────────────────────────
# POST /logout
# ──────────────────────────
@bp.route("/logout", methods=["POST"])
@jwt_required()
def logout():
    try:
        user_id = get_jwt_identity()
        blacklist_jwt()
        log_action(user_id, "logout")
        publish_event("auth.events", {"event": "user_logged_out", "user_id": user_id})
        return jsonify({"success": True, "message": "Logged out"}), 200
    except Exception as e:
        logger.warning(f"Logout issue: {str(e)}")
        return jsonify({"success": True}), 200


# ──────────────────────────
# POST /verify-email
# ──────────────────────────
@bp.route("/verify-email", methods=["POST"])
def verify_email():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if not token:
        return jsonify({"error": "Verification token required"}), 400
    try:
        verified = supabase.auth.verify_otp({"token_hash": token, "type": "signup"})
        if not verified.user:
            return jsonify({"error": "Invalid or expired token"}), 400
        user_id = verified.user.id
        supabase.table("profiles").update({"email_verified": True, "updated_at": "now()"}).eq("id", user_id).execute()
        access, refresh = generate_tokens(user_id)
        log_action(user_id, "email_verified")
        publish_event("auth.events", {"event": "email_verified", "user_id": user_id})
        return jsonify({"success": True, "message": "Email verified successfully", "access_token": access, "refresh_token": refresh}), 200
    except Exception as e:
        logger.error(f"Email verification failed: {str(e)}")
        return jsonify({"error": "Verification failed"}), 400


# ──────────────────────────
# POST /reset-password/confirm
# ──────────────────────────
@bp.route("/reset-password/confirm", methods=["POST"])
def reset_password_confirm():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    password = data.get("password")
    if not token or not password:
        return jsonify({"error": "Token and new password required"}), 400
    try:
        res = supabase.auth.update_user({"password": password})
        if res.user:
            log_action(res.user.id, "password_reset_confirmed")
            publish_event("auth.events", {"event": "password_reset", "user_id": res.user.id})
            return jsonify({"message": "Password reset successful"}), 200
        else:
            return jsonify({"error": "Invalid or expired token"}), 400
    except Exception as e:
        logger.error(f"Reset confirm error: {str(e)}")
        return jsonify({"error": "Failed to reset password"}), 500
