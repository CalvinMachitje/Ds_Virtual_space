# services/auth-service/app/routes/auth/twofa.py
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
import logging
from app.services.supabase_service import supabase
from app.utils.audit import log_action
from app.utils.event_bus import publish_event  # Redis event publisher

bp = Blueprint("auth_twofa_bp", __name__, url_prefix="/2fa")
logger = logging.getLogger(__name__)

@bp.route("/ping")
def ping():
    logger.info("Ping endpoint called")
    # Use current_app inside function if needed
    current_app.logger.debug("Debug log inside route")
    return jsonify({"pong": True})

# ──────────────────────────
# POST /2fa/setup
# ──────────────────────────
@bp.route("/2fa/setup", methods=["POST"])
@jwt_required()
def setup_2fa():
    user_id = get_jwt_identity()
    try:
        factor = supabase.auth.mfa.enroll({
            "factor_type": "totp",
            "issuer": "D's Virtual Space",
            "user_id": user_id
        })
        if not factor:
            return jsonify({"error": "Failed to start 2FA setup"}), 500

        # Publish event: 2FA setup started
        publish_event("auth.events", {
            "event": "2fa_setup_started",
            "user_id": user_id
        })

        return jsonify({
            "success": True,
            "qr_code": factor.totp.qr_code,
            "secret": factor.totp.secret,
            "factor_id": factor.id
        }), 200
    except Exception as e:
        logger.error(f"2FA setup failed for {user_id}: {str(e)}")
        return jsonify({"error": "Failed to setup 2FA"}), 500

# ──────────────────────────
# POST /2fa/verify
# ──────────────────────────
@bp.route("/2fa/verify", methods=["POST"])
@jwt_required()
def verify_2fa():
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    code = data.get("code")
    factor_id = data.get("factor_id")
    if not code or not factor_id:
        return jsonify({"error": "Code and factor_id required"}), 400

    try:
        verified = supabase.auth.mfa.verify({"factor_id": factor_id, "code": code})
        if not verified:
            return jsonify({"error": "Invalid 2FA code"}), 401

        supabase.table("profiles").update({"two_factor_enabled": True, "updated_at": "now()"}).eq("id", user_id).execute()
        log_action(user_id, "2fa_enabled")

        # Publish event: 2FA enabled
        publish_event("auth.events", {
            "event": "2fa_enabled",
            "user_id": user_id
        })

        return jsonify({"success": True, "message": "2FA enabled"}), 200
    except Exception as e:
        logger.error(f"2FA verify failed for {user_id}: {str(e)}")
        return jsonify({"error": "Verification failed"}), 400

# ──────────────────────────
# POST /2fa/disable
# ──────────────────────────
@bp.route("/2fa/disable", methods=["POST"])
@jwt_required()
def disable_2fa():
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    code = data.get("code")
    if not code:
        return jsonify({"error": "2FA code required to disable"}), 400

    try:
        factors = supabase.auth.mfa.list_user_factors()
        totp_factor = next((f for f in factors if f.factor_type == "totp"), None)
        if not totp_factor:
            return jsonify({"error": "No 2FA enabled"}), 400

        verified = supabase.auth.mfa.verify({"factor_id": totp_factor.id, "code": code})
        if not verified:
            return jsonify({"error": "Invalid code"}), 401

        supabase.auth.mfa.unenroll(totp_factor.id)
        supabase.table("profiles").update({"two_factor_enabled": False, "updated_at": "now()"}).eq("id", user_id).execute()
        log_action(user_id, "2fa_disabled")

        # Publish event: 2FA disabled
        publish_event("auth.events", {
            "event": "2fa_disabled",
            "user_id": user_id
        })

        return jsonify({"success": True, "message": "2FA disabled"}), 200
    except Exception as e:
        logger.error(f"2FA disable failed for {user_id}: {str(e)}")
        return jsonify({"error": "Failed to disable 2FA"}), 400

# ──────────────────────────
# Optional helper for login route
# ──────────────────────────
def verify_2fa_code(user_id: str, code: str) -> bool:
    try:
        factors = supabase.auth.mfa.list_user_factors()
        totp_factor = next((f for f in factors if f.factor_type == "totp"), None)
        if not totp_factor:
            return False
        verified = supabase.auth.mfa.verify({"factor_id": totp_factor.id, "code": code})
        if verified:
            # Publish event: 2FA code verified (for login flow)
            publish_event("auth.events", {
                "event": "2fa_verified",
                "user_id": user_id
            })
        return bool(verified)
    except Exception as e:
        logger.error(f"verify_2fa_code failed for {user_id}: {str(e)}")
        return False