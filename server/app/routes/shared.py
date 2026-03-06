# app/routes/shared.py
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import get_jwt, jwt_required, get_jwt_identity
from app.services.supabase_service import supabase
from datetime import datetime
import re, logging
from app.utils.audit import log_action
from app.config import ALLOWED_REDIRECT_DOMAINS 
from app.extensions import safe_redis_call, limiter

bp = Blueprint("shared", __name__, url_prefix="/api")
logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────
# GET /api/gigs
# Public: List published gigs (paginated + basic filters)
# ────────────────────────────────────────────────
@bp.route("/gigs", methods=["GET"])
def list_gigs():
    """
    Public endpoint: Browse all published gigs
    Query params: page, limit, search, category
    """
    page = request.args.get("page", 0, type=int)
    limit = request.args.get("limit", 9, type=int)
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()

    from_idx = page * limit
    to_idx = from_idx + limit - 1

    try:
        query = supabase.table("gigs")\
            .select("""
                id,
                title,
                description,
                price,
                category,
                gallery_urls,
                created_at,
                seller:seller_id (full_name, avatar_url)
            """)\
            .eq("status", "published")\
            .order("created_at", desc=True)

        if search and len(search) >= 2:
            query = query.or_(
                f"title.ilike.%{search}%,description.ilike.%{search}%"
            )

        if category:
            query = query.eq("category", category)

        res = query.range(from_idx, to_idx).execute()

        gigs = []
        for gig in (res.data or []):
            seller = gig.pop("seller", {}) or {}
            gigs.append({
                **gig,
                "seller_name": seller.get("full_name", "Unknown"),
                "seller_avatar": seller.get("avatar_url"),
            })

        return jsonify({
            "gigs": gigs,
            "total": res.count or 0,
            "page": page,
            "limit": limit,
            "has_more": len(gigs) == limit
        }), 200

    except Exception as e:
        current_app.logger.exception("Public gigs fetch failed")
        return jsonify({"error": "Failed to load gigs"}), 500


# GET /api/gigs/:id
@bp.route("/gigs/<string:id>", methods=["GET"])
def get_gig(id: str):
    """
    Public endpoint: Fetch details for a single published gig
    """
    try:
        res = supabase.table("gigs")\
            .select("""
                id,
                title,
                description,
                price,
                category,
                gallery_urls,
                created_at,
                seller:seller_id (full_name, avatar_url, is_verified)
            """)\
            .eq("id", id)\
            .eq("status", "published")\
            .maybe_single()\
            .execute()

        if not res.data:
            return jsonify({"error": "Gig not found or not published"}), 404

        gig = res.data
        seller = gig.pop("seller", {}) or {}

        return jsonify({
            **gig,
            "seller_name": seller.get("full_name", "Unknown"),
            "seller_avatar": seller.get("avatar_url"),
            "seller_is_verified": seller.get("is_verified", False),
        }), 200

    except Exception as e:
        current_app.logger.exception(f"Gig fetch failed for ID {id}")
        return jsonify({"error": "Failed to load gig"}), 500


# ────────────────────────────────────────────────
# POST /api/auth/forgot-password
# Send password reset email (rate-limited)
# ────────────────────────────────────────────────
@bp.route("/auth/forgot-password", methods=["POST"])
@limiter.limit("3 per hour")  # prevent abuse
def forgot_password():
    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()

    if not email or not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({"error": "Valid email required"}), 400

    try:
        # Optional: check if user exists (privacy: don't reveal)
        user_check = supabase.table("profiles").select("id").eq("email", email).maybe_single().execute()
        if not user_check.data:
            # Still send (prevents email enumeration attack)
            pass

        redirect_to = request.args.get("redirect_to", f"{request.host_url}reset-password")
        allowed = any(domain in redirect_to.lower() for domain in ALLOWED_REDIRECT_DOMAINS)

        if not allowed:
            current_app.logger.warning(f"Invalid redirect domain: {redirect_to}")
            redirect_to = f"{request.host_url}reset-password"

        supabase.auth.reset_password_for_email(
            email,
            redirect_to=redirect_to
        )

        # Optional: log attempt
        log_action(None, "forgot_password_attempt", details={"email": email})

        return jsonify({"message": "If the email exists, a reset link has been sent"}), 200

    except Exception as e:
        current_app.logger.error(f"Forgot password error (email {email}): {str(e)}")
        return jsonify({"error": "Failed to process request"}), 500


# ────────────────────────────────────────────────
# POST /api/auth/reset-password
# Reset password with token
# ────────────────────────────────────────────────
@bp.route("/auth/reset-password", methods=["POST"])
@limiter.limit("5 per hour")
def reset_password():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    password = data.get("password", "").strip()

    if not token or not password:
        return jsonify({"error": "Token and password required"}), 400

    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    try:
        res = supabase.auth.update_user({"password": password})

        if res.user:
            # Optional: log success
            log_action(res.user.id, "password_reset_success")
            return jsonify({"message": "Password reset successful"}), 200
        else:
            return jsonify({"error": "Invalid or expired token"}), 400

    except Exception as e:
        current_app.logger.error(f"Reset password error: {str(e)}")
        return jsonify({"error": "Failed to reset password"}), 500


# ────────────────────────────────────────────────
# GET /api/auth/session
# Get current session info (authenticated)
# ────────────────────────────────────────────────
@bp.route("/auth/session", methods=["GET"])
@jwt_required()
def get_session():
    user_id = get_jwt_identity()

    try:
        profile = supabase.table("profiles")\
            .select("email, full_name, avatar_url, last_sign_in_at")\
            .eq("id", user_id)\
            .single()\
            .execute()\
            .data

        if not profile:
            return jsonify({"error": "User not found"}), 404

        return jsonify(profile), 200

    except Exception as e:
        current_app.logger.error(f"Session fetch error (user {user_id}): {str(e)}")
        return jsonify({"error": "Failed to fetch session"}), 500


# ────────────────────────────────────────────────
# POST /api/auth/logout
# Server-side logout with token blocklist
# ────────────────────────────────────────────────
@bp.route("/auth/logout", methods=["POST"])
@jwt_required()
def logout():
    try:
        jti = get_jwt()["jti"]
        expires = get_jwt()["exp"] - int(datetime.utcnow().timestamp()) + 3600  # remaining time + buffer
        safe_redis_call("setex", f"blacklist:{jti}", expires, "true")

        return jsonify({"message": "Logged out successfully"}), 200

    except Exception as e:
        current_app.logger.warning(f"Logout issue: {str(e)}")
        return jsonify({"message": "Logged out"}), 200


# ────────────────────────────────────────────────
# GET /api/debug/supabase
# Simple connection check (remove or protect in production)
# ────────────────────────────────────────────────
@bp.route("/debug/supabase", methods=["GET"])
def debug_supabase():
    try:
        # Simple test query
        test = supabase.table("profiles").select("count(*)", count="exact").execute()
        status = {
            "connected": True,
            "row_count_profiles": test.count or 0,
            "timestamp": datetime.utcnow().isoformat()
        }
        return jsonify(status), 200
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)}), 500