# services/auth-service/app/routes/auth/admin.py
from flask import Blueprint, request, jsonify
import logging

from app.services.supabase_service import supabase
from app.utils.audit import log_action
from app.extensions import limiter, safe_redis_call
from app.constants import ADMIN_FAIL_THRESHOLD, ADMIN_LOCKOUT_MINUTES, RATE_LIMIT_ADMIN_LOGIN
from app.utils.utils import generate_tokens
from app.utils.event_bus import publish_event

bp = Blueprint("auth_admin_bp", __name__, url_prefix="/admin")
logger = logging.getLogger(__name__)


# ──────────────────────────
# POST /admin-login
# ──────────────────────────
@bp.route("/admin-login", methods=["POST"])
@limiter.limit(RATE_LIMIT_ADMIN_LOGIN)
def admin_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    otp = data.get("otp")
    ip = request.remote_addr or "unknown"

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    fail_key = f"admin_fail:{email}:{ip}"
    lock_key = f"admin_lock:{email}:{ip}"

    # 🔒 Hard lock check
    if safe_redis_call("exists", lock_key):
        ttl = safe_redis_call("ttl", lock_key, default=0)
        return jsonify({"error": f"Too many failed attempts. Locked for {ttl // 60 + 1} min"}), 429

    # 🔒 Short request lock (prevent spam clicks)
    acquired = safe_redis_call("setnx", lock_key, "1")
    if not acquired:
        ttl = safe_redis_call("ttl", lock_key, default=0)
        return jsonify({"error": f"Too many login attempts. Locked for {ttl // 60 + 1} sec"}), 429

    safe_redis_call("expire", lock_key, 10)  # 10 second short lock

    try:
        # ✅ Use safe_auth method from service
        auth_resp = supabase.admin_login(email, password, otp)

        # 🔴 Handle errors from service
        if not auth_resp or auth_resp.get("error"):
            fails = safe_redis_call("incr", fail_key, default=0) or 1
            safe_redis_call("expire", fail_key, 1800)  # 30 min TTL

            if fails >= ADMIN_FAIL_THRESHOLD:
                safe_redis_call("setex", lock_key, ADMIN_LOCKOUT_MINUTES * 60, "locked")
                log_action(None, "admin_account_locked", {
                    "email": email,
                    "ip": ip,
                    "fails": fails
                })

            return jsonify({"error": auth_resp.get("error", "Login failed")}), 401

        # 🟡 Handle 2FA requirement
        if auth_resp.get("2fa_required"):
            return jsonify({"2fa_required": True}), 200

        user = auth_resp.get("user")
        if not user:
            return jsonify({"error": "Authentication failed"}), 401

        # 🔐 Check admin table
        admin_res = supabase.table("admins").select("*").eq("id", user["id"]).single().execute()
        admin = admin_res.data

        if not admin:
            return jsonify({"error": "Not registered as admin"}), 403

        if not user.get("email_confirmed_at"):
            return jsonify({
                "error": "Email not confirmed",
                "needs_confirmation": True
            }), 403

        # 🔑 Generate tokens
        access, refresh = generate_tokens(
            str(user["id"]),
            {"role": "admin", "admin_level": admin["admin_level"]}
        )

        # 🧾 Logging + events
        log_action(user["id"], "admin_login_success", {
            "email": email,
            "admin_level": admin["admin_level"],
            "ip": ip
        })

        publish_event("auth.events", {
            "event": "admin_login",
            "admin_id": user["id"],
            "email": email,
            "admin_level": admin["admin_level"],
            "ip": ip
        })

        # ✅ Clear fail counters
        safe_redis_call("delete", fail_key)
        safe_redis_call("delete", lock_key)

        return jsonify({
            "success": True,
            "access_token": access,
            "refresh_token": refresh,
            "user": {
                **admin,
                "email": user["email"],
                "role": "admin"
            }
        }), 200

    except Exception as e:
        logger.exception(f"Admin login error {email}")

        fails = safe_redis_call("incr", fail_key, default=0) or 1
        safe_redis_call("expire", fail_key, 1800)

        if fails >= ADMIN_FAIL_THRESHOLD:
            safe_redis_call("setex", lock_key, ADMIN_LOCKOUT_MINUTES * 60, "locked")
            log_action(None, "admin_account_locked", {
                "email": email,
                "ip": ip,
                "fails": fails
            })

        return jsonify({"error": "Authentication failed"}), 401

    finally:
        # Always remove short request lock
        safe_redis_call("delete", lock_key)