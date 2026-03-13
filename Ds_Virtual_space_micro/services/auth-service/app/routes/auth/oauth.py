# services/auth-service/app/routes/auth/oauth.py
from flask import Blueprint, request, jsonify, current_app
import logging
from app.services.supabase_service import supabase
from app.utils.audit import log_action
from flask_jwt_extended import create_access_token, create_refresh_token
from app.utils.event_bus import publish_event  # Redis event bus

bp = Blueprint("auth_oauth_bp", __name__, url_prefix="/oauth")
logger = logging.getLogger(__name__)

@bp.route("/ping")
def ping():
    logger.info("Ping endpoint called")
    # Use current_app inside function if needed
    current_app.logger.debug("Debug log inside route")
    return jsonify({"pong": True})

# ──────────────────────────
# POST /oauth/<provider>
# ──────────────────────────
@bp.route("/oauth/<provider>", methods=["POST"])
def start_oauth(provider: str):
    if provider not in ["google", "facebook"]:
        return jsonify({"error": "Unsupported provider"}), 400

    data = request.get_json(silent=True) or {}
    frontend_redirect = data.get("redirect_to") or f"{request.host_url.rstrip('/')}/oauth/callback"

    try:
        oauth_response = supabase.auth.sign_in_with_oauth({
            "provider": provider,
            "options": {
                "redirect_to": frontend_redirect,
                "scopes": "email profile" if provider == "google" else "email public_profile"
            }
        })
        oauth_url = getattr(oauth_response, "url", None)
        if not oauth_url:
            return jsonify({"error": "Failed to generate OAuth URL"}), 500

        logger.info(f"Generated {provider} OAuth URL: {oauth_url}")
        # Publish event: OAuth flow started
        publish_event("auth.events", {
            "event": "oauth_started",
            "provider": provider,
            "redirect_to": frontend_redirect
        })

        return jsonify({"success": True, "oauth_url": oauth_url, "provider": provider, "redirect_to": frontend_redirect}), 200
    except Exception as e:
        logger.exception(f"Failed to generate {provider} OAuth URL")
        return jsonify({"error": "Failed to generate OAuth URL", "details": str(e)}), 500

# ──────────────────────────
# POST /oauth/callback
# ──────────────────────────
@bp.route("/oauth/callback", methods=["POST"])
def oauth_callback():
    data = request.get_json(silent=True) or {}
    code = data.get("code")
    provider = data.get("provider")
    if not code or not provider:
        return jsonify({"error": "Authorization code and provider required"}), 400

    try:
        session = supabase.auth.exchange_code_for_session(code)
        if not session or not session.user:
            return jsonify({"error": "OAuth authentication failed"}), 401

        user = session.user
        profile_res = supabase.table("profiles").select("*").eq("id", user.id).maybe_single().execute()
        profile = profile_res.data
        if not profile:
            full_name = user.user_metadata.get("full_name") or user.user_metadata.get("name") or user.email.split("@")[0].title()
            profile = {
                "id": user.id,
                "email": user.email,
                "full_name": full_name,
                "avatar_url": user.user_metadata.get("avatar_url"),
                "role": "buyer",
                "created_at": "now()",
                "updated_at": "now()"
            }
            supabase.table("profiles").insert(profile).execute()
            # Publish event: new OAuth user created
            publish_event("auth.events", {
                "event": "oauth_user_registered",
                "user_id": user.id,
                "email": user.email,
                "full_name": full_name,
                "role": "buyer",
                "provider": provider
            })

        access_token = create_access_token(identity=user.id)
        refresh_token = create_refresh_token(identity=user.id)
        logger.info(f"Successful {provider} OAuth login | user_id={user.id} | email={user.email}")
        log_action(user.id, "oauth_login", {"provider": provider})

        # Publish event: OAuth login success
        publish_event("auth.events", {
            "event": "oauth_login_success",
            "user_id": user.id,
            "provider": provider
        })

        return jsonify({"success": True, "access_token": access_token, "refresh_token": refresh_token, "user": profile}), 200
    except Exception as e:
        logger.exception(f"OAuth callback failed for {provider}")
        return jsonify({"error": "Failed to complete OAuth login", "details": str(e)}), 500