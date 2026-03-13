# services/auth-service/app/routes/auth/admin.py
from flask import Blueprint, current_app, request, jsonify
import logging
from app.services.supabase_service import supabase
from app.utils.audit import log_action
from app.extensions import limiter, safe_redis_call
from app.utils.event_bus import publish_event
from app.routes.auth.constants import ADMIN_FAIL_THRESHOLD, ADMIN_LOCKOUT_MINUTES, RATE_LIMIT_ADMIN_LOGIN
from app.utils.utils import generate_tokens

bp = Blueprint("auth_routes", __name__)
logger = logging.getLogger(__name__)

@bp.route("/ping")
def ping():
    logger.info("Ping endpoint called")
    # Use current_app inside function if needed
    current_app.logger.debug("Debug log inside route")
    return jsonify({"pong": True})

# ──────────────────────────
# POST /admin-login
# ──────────────────────────
@bp.route("/admin-login", methods=["POST"])
@limiter.limit(RATE_LIMIT_ADMIN_LOGIN)
def admin_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    otp = data.get("otp")  # Optional for future 2FA
    ip = request.remote_addr

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    fail_key = f"admin_fail:{email}:{ip}"
    lock_key = f"admin_lock:{email}:{ip}"

    # Check if account is temporarily locked
    if safe_redis_call("exists", lock_key):
        ttl = safe_redis_call("ttl", lock_key, default=0)
        return jsonify({"error": f"Too many failed attempts. Locked for {ttl // 60 + 1} min"}), 429

    try:
        auth_res = supabase.auth.sign_in_with_password({"email": email, "password": password})
        user = auth_res.user
        if not user:
            # Increment fail counter
            fails = safe_redis_call("incr", fail_key, default=0) or 1
            safe_redis_call("expire", fail_key, 1800)  # 30 min window
            if fails >= ADMIN_FAIL_THRESHOLD:
                safe_redis_call("setex", lock_key, ADMIN_LOCKOUT_MINUTES * 60, "locked")
                log_action(None, "admin_account_locked", {"email": email, "ip": ip, "fails": fails})
            return jsonify({"error": "Invalid email or password"}), 401

        # Fetch admin details
        admin_res = supabase.table("admins").select("*").eq("id", user.id).single().execute()
        admin = admin_res.data
        if not admin:
            return jsonify({"error": "Not registered as admin"}), 403

        # Ensure email is confirmed
        if not user.email_confirmed_at:
            return jsonify({"error": "Email not confirmed", "needs_confirmation": True}), 403

        # Generate JWT tokens with admin role
        access, refresh = generate_tokens(str(user.id), {"role": "admin", "admin_level": admin["admin_level"]})
        log_action(user.id, "admin_login_success", {"email": email, "admin_level": admin["admin_level"], "ip": ip})

        # Publish admin login event
        publish_event("auth.events", {
            "event": "admin_login",
            "admin_id": user.id,
            "email": email,
            "admin_level": admin["admin_level"],
            "ip": ip
        })

        # Clear fail counters on successful login
        safe_redis_call("del", fail_key)
        safe_redis_call("del", lock_key)

        return jsonify({
            "success": True,
            "access_token": access,
            "refresh_token": refresh,
            "user": {**admin, "email": user.email, "role": "admin"}
        }), 200

    except Exception as e:
        logger.exception(f"Admin login error {email}")
        return jsonify({"error": "Authentication failed"}), 500