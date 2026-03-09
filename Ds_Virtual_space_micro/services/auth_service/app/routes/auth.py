# services/auth_service/app/routes/auth.py
from flask import Blueprint, current_app, request, jsonify
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    jwt_required,
    get_jwt_identity,
    get_jwt
)
from supabase_service import supabase
from datetime import datetime, timedelta
import re, logging, time
from utils.audit import log_action
from extensions import safe_redis_call, limiter   
from flask_cors import cross_origin


bp = Blueprint("auth", __name__, url_prefix="/api/auth")
logger = logging.getLogger(__name__)


def is_strong_password(password: str) -> tuple[bool, str]:
    """Return (is_valid, error_message)"""
    if len(password) < 10:
        return False, "Password must be at least 10 characters"
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r"[0-9]", password):
        return False, "Password must contain at least one number"
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        return False, "Password must contain at least one special character"
    return True, ""


# ────────────────────────────────
# POST /api/auth/signup
# ────────────────────────────────
@bp.route("/signup", methods=["POST"])
@limiter.limit("3 per minute")  # prevent spam
def signup():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    full_name = (data.get("full_name") or "").strip()
    phone = (data.get("phone") or "").strip()
    role = (data.get("role") or "").strip().lower()

    if not all([email, password, full_name, role]):
        return jsonify({"error": "Missing required fields"}), 400

    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({"error": "Invalid email format"}), 400

    if role not in ["buyer", "seller"]:
        return jsonify({"error": "Role must be 'buyer' or 'seller'"}), 400

    is_valid_pw, pw_msg = is_strong_password(password)
    if not is_valid_pw:
        return jsonify({"error": pw_msg}), 400

    try:
        # Check if email exists
        existing = supabase.table("profiles").select("id").eq("email", email).maybe_single().execute()
        if existing.data:
            return jsonify({"error": "Email already registered"}), 409

        sign_up = supabase.auth.sign_up({
            "email": email,
            "password": password,
            "options": {"data": {"full_name": full_name, "phone": phone, "role": role}}
        })

        user = sign_up.user
        if not user:
            return jsonify({"error": "User creation failed"}), 500

        # Insert profile
        profile_data = {
            "id": user.id,
            "full_name": full_name,
            "email": email,
            "phone": phone,
            "role": role,
            "created_at": "now()"
        }
        supabase.table("profiles").insert(profile_data).execute()

        log_action(
            actor_id=user.id,
            action="signup",
            details={"email": email, "role": role}
        )

        if not sign_up.session:
            return jsonify({
                "success": True,
                "message": "Check your email to confirm account",
                "email_confirmation_sent": True
            }), 200

        access = create_access_token(identity=user.id)
        refresh = create_refresh_token(identity=user.id)

        return jsonify({
            "success": True,
            "access_token": access,
            "refresh_token": refresh,
            "user": {
                "id": user.id,
                "email": email,
                "full_name": full_name,
                "role": role,
                "phone": phone
            }
        }), 201

    except Exception as e:
        logger.error(f"Signup error for {email}: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to create account"}), 500


# ───────────────────────────────────────────────────
# POST /api/auth/login  (regular users: buyer/seller)
# ───────────────────────────────────────────────────
@bp.route("/login", methods=["POST"])
@cross_origin(origins=["http://localhost:5173", "*"])
@limiter.limit("5 per minute; 20 per hour")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    otp = data.get("otp")  # optional: 2FA code

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    ip = request.remote_addr
    fail_key = f"login_fail:{email}:{ip}"
    lock_key = f"login_lock:{email}:{ip}"

    # 1. Check if account is locked
    if safe_redis_call("exists", lock_key):
        ttl = safe_redis_call("ttl", lock_key, default=0)
        return jsonify({
            "error": f"Too many failed attempts. Try again in {ttl // 60 + 1} minutes"
        }), 429

    try:
        auth_resp = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })

        if not auth_resp.user:
            # Failed attempt → increment counter
            fails = safe_redis_call("incr", fail_key, default=0)
            safe_redis_call("expire", fail_key, 3600)  # 1 hour window

            if fails >= 10:
                safe_redis_call("setex", lock_key, 3600, "locked")  # 1 hour lockout
                log_action(None, "account_locked", details={"email": email, "ip": ip, "fails": fails})
                return jsonify({
                    "error": "Too many failed attempts. Account locked for 1 hour"
                }), 429

            log_action(None, "failed_login", details={"email": email, "ip": ip, "attempt": fails})
            return jsonify({"error": "Invalid email or password"}), 401

        user = auth_resp.user

        # Fetch profile
        profile_res = supabase.table("profiles")\
            .select("id, full_name, role, avatar_url, phone, is_verified, banned, two_factor_enabled")\
            .eq("id", user.id)\
            .maybe_single().execute()

        profile = profile_res.data if profile_res and profile_res.data else {}

        # Block banned users
        if profile.get("banned", False):
            return jsonify({"error": "Account is banned"}), 403

        # Require email confirmation for new accounts
        if not user.email_confirmed_at:
            return jsonify({
                "error": "Please confirm your email first. Check your inbox.",
                "needs_confirmation": True
            }), 403

        # 2FA enforcement (if enabled)
        if profile.get("two_factor_enabled", False) and not otp:
            # Tell frontend to prompt for 2FA code
            return jsonify({
                "success": True,
                "requires_2fa": True,
                "message": "2FA code required",
                "factor_id": "totp"  # or fetch real factor_id if multiple
            }), 200

        if profile.get("two_factor_enabled", False) and otp:
            # Verify 2FA code
            try:
                verified = supabase.auth.mfa.verify({
                    "factor_id": "totp",  # adjust if you store factor_id in profile
                    "code": otp
                })
                if not verified:
                    return jsonify({"error": "Invalid 2FA code"}), 401
            except Exception as e:
                logger.error(f"2FA verification failed: {str(e)}")
                return jsonify({"error": "2FA verification failed"}), 401

        # Reset failed attempts on success
        safe_redis_call("delete", fail_key)
        safe_redis_call("delete", lock_key)

        access = create_access_token(identity=user.id)
        refresh = create_refresh_token(identity=user.id)

        log_action(
            actor_id=user.id,
            action="user_login",
            details={"email": email, "role": profile.get("role", "unknown")}
        )

        return jsonify({
            "success": True,
            "access_token": access,
            "refresh_token": refresh,
            "user": {
                "id": user.id,
                "email": user.email,
                "email_confirmed": bool(user.email_confirmed_at),
                **profile
            }
        }), 200

    except Exception as e:
        error_str = str(e).lower()
        logger.error(f"Login failed for {email}: {error_str}", exc_info=True)

        # Count failed attempt even on exception
        fails = safe_redis_call("incr", fail_key, default=0)
        safe_redis_call("expire", fail_key, 3600)
        if fails >= 10:
            safe_redis_call("setex", lock_key, 3600, "locked")

        return jsonify({"error": "Login failed. Please try again later."}), 500


# ────────────────────────────────────────────────
# POST /api/auth/admin-login  (admin only)
# ────────────────────────────────────────────────
@bp.route("/admin-login", methods=["POST", "OPTIONS"])
@cross_origin(origins=["http://localhost:5173", "*"])
@limiter.limit("3 per minute; 10 per hour")
def admin_login():
    """
    Admin-only login endpoint - FIXED for your exact schema
    """
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    otp = data.get("otp")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    ip = request.remote_addr
    fail_key = f"admin_fail:{email}:{ip}"
    lock_key = f"admin_lock:{email}:{ip}"

    # Check if account is locked
    if safe_redis_call("exists", lock_key, default=False):
        ttl = safe_redis_call("ttl", lock_key, default=0)
        return jsonify({
            "error": f"Too many failed attempts. Locked for {ttl // 60 + 1} minutes"
        }), 429

    try:
        # 1. Supabase Auth
        auth_res = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })

        user = getattr(auth_res, "user", None)
        if not user:
            # Increment failure counter
            fails = safe_redis_call("incr", fail_key, default=0) or 0
            safe_redis_call("expire", fail_key, 1800)

            if fails >= 5:
                safe_redis_call("setex", lock_key, 1800, "locked")
                log_action(None, "admin_account_locked", details={
                    "email": email, "ip": ip, "fails": fails
                })
                return jsonify({"error": "Too many failed attempts. Locked for 30 minutes"}), 429

            log_action(None, "failed_admin_login", details={"email": email, "ip": ip})
            logger.warning(f"Admin login failed for {email}: invalid credentials")
            return jsonify({"error": "Invalid email or password"}), 401

        # 2. FIXED: Safe admin verification with try/catch
        admin = None
        try:
            # Try single record fetch - fails cleanly on no record
            admin_res = supabase.table("admins")\
                .select("admin_level, permissions, last_login")\
                .eq("id", user.id)\
                .single()\
                .execute()
            admin = admin_res.data
            
        except Exception as admin_error:
            error_str = str(admin_error).lower()
            
            # Handle all "no record" cases (PGRST116=not found, 204, 406, etc.)
            if any(x in error_str for x in ["pgrst116", "not found", "no rows", "204", "406"]):
                log_action(None, "admin_login_denied", details={
                    "email": email,
                    "reason": "no_admin_record",
                    "user_id": str(user.id)
                })
                logger.info(f"No admin record for {email} (id: {user.id})")
                return jsonify({
                    "error": "This account is not registered as an admin. Contact support."
                }), 403
            
            # Log real Supabase errors
            logger.error(f"Admin query failed for {user.id}: {admin_error}")
            return jsonify({"error": "Admin verification failed - server error"}), 500

        # 3. FIXED: Fetch profile (MATCHES YOUR EXACT SCHEMA - NO email_confirmed_at)
        profile_res = supabase.table("profiles")\
            .select("full_name, avatar_url, two_factor_enabled")\
            .eq("id", user.id)\
            .single()\
            .execute()
        profile = getattr(profile_res, 'data', {})

        # 4. Email confirmation check - USE Supabase auth.user (ALWAYS exists)
        if not user.email_confirmed_at:
            return jsonify({
                "error": "Admin email not confirmed. Check your inbox.",
                "needs_confirmation": True
            }), 403

        # 5. 2FA (if enabled)
        if profile.get("two_factor_enabled", False) and not otp:
            return jsonify({
                "success": True,
                "requires_2fa": True,
                "message": "2FA code required"
            }), 200

        # 6. SUCCESS - Reset counters
        safe_redis_call("del", fail_key)
        safe_redis_call("del", lock_key)

        # Update last login
        supabase.table("admins")\
            .update({"last_login": "now()"})\
            .eq("id", user.id)\
            .execute()

        # Generate JWT with admin claims
        access = create_access_token(
            identity=user.id,
            additional_claims={
                "role": "admin",
                "admin_level": admin["admin_level"]
            }
        )
        refresh = create_refresh_token(identity=user.id)

        # Audit success
        log_action(user.id, "admin_login_success", details={
            "email": email,
            "admin_level": admin["admin_level"],
            "ip": ip
        })

        return jsonify({
            "success": True,
            "access_token": access,
            "refresh_token": refresh,
            "user": {
                "id": user.id,
                "email": user.email,
                "role": "admin",
                "admin_level": admin["admin_level"],
                "permissions": admin.get("permissions", {}),
                "last_login": admin.get("last_login"),
                **profile
            }
        }), 200

    except Exception as e:
        error_str = str(e)
        logger.exception(f"Unexpected admin login error for {email}")

        # Safe failure counting
        fails = safe_redis_call("incr", fail_key, default=0) or 0
        safe_redis_call("expire", fail_key, 1800)
        if fails >= 5:
            safe_redis_call("setex", lock_key, 1800, "locked")

        log_action(None, "failed_admin_login", details={
            "email": email,
            "reason": error_str[:200],
            "ip": ip
        })

        if "invalid login credentials" in error_str.lower():
            return jsonify({"error": "Invalid email or password"}), 401

        return jsonify({
            "error": "Authentication failed. Please try again.",
            "details": error_str[:100] if current_app.debug else None
        }), 500

# ────────────────────────────────
# POST /api/auth/logout
# ────────────────────────────────
@bp.route("/logout", methods=["POST"])
@jwt_required()
def logout():
    try:
        jti = get_jwt()["jti"]
        expires = get_jwt()["exp"] - int(datetime.utcnow().timestamp()) + 3600
        safe_redis_call("setex", f"blacklist:{jti}", expires, "true")

        log_action(
            actor_id=get_jwt_identity(),
            action="logout"
        )

        return jsonify({"success": True, "message": "Logged out successfully"}), 200

    except Exception as e:
        logger.warning(f"Logout issue: {str(e)}")
        return jsonify({"success": True, "message": "Logged out"}), 200


# ────────────────────────────────
# POST /api/auth/refresh
# ────────────────────────────────
@bp.route("/refresh", methods=["POST"])
@jwt_required(refresh=True)
def refresh_token():
    current_user_id = get_jwt_identity()
    new_access_token = create_access_token(identity=current_user_id)

    return jsonify({
        "success": True,
        "access_token": new_access_token
    }), 200


# ────────────────────────────────
# GET /api/auth/me
# ────────────────────────────────
@bp.route("/me", methods=["GET"])
@jwt_required()
def get_current_user():
    user_id = get_jwt_identity()
    try:
        profile = supabase.table("profiles").select("*").eq("id", user_id).maybe_single().execute().data

        if not profile:
            return jsonify({"error": "Profile not found"}), 404

        return jsonify({"success": True, "user": profile}), 200

    except Exception as e:
        logger.error(f"/me failed for {user_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to fetch user info"}), 500


# ────────────────────────────────
# GET /api/auth/debug/supabase
# ────────────────────────────────
@bp.route("/debug/supabase", methods=["GET"])
@jwt_required()
def debug_supabase():
    try:
        test = supabase.table("profiles").select("count(*)", count="exact").execute()
        status = {
            "connected": True,
            "row_count_profiles": test.count or 0,
            "timestamp": datetime.utcnow().isoformat()
        }
        return jsonify(status), 200
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)}), 500
    
# POST /api/auth/verify-email
@bp.route("/verify-email", methods=["POST"])
def verify_email():
    data = request.get_json(silent=True) or {}
    token = data.get("token")

    if not token:
        return jsonify({"error": "Verification token required"}), 400

    try:
        # Supabase handles email confirmation token verification
        verified = supabase.auth.verify_otp({
            "token_hash": token,
            "type": "signup"
        })

        if not verified.user:
            return jsonify({"error": "Invalid or expired token"}), 400

        user_id = verified.user.id

        # Update profile to mark as verified (if you have such a field)
        supabase.table("profiles")\
            .update({"email_verified": True, "updated_at": "now()"})\
            .eq("id", user_id)\
            .execute()

        # Optional: create access/refresh tokens after confirmation
        access = create_access_token(identity=user_id)
        refresh = create_refresh_token(identity=user_id)

        log_action(user_id, "email_verified")

        return jsonify({
            "success": True,
            "message": "Email verified successfully",
            "access_token": access,
            "refresh_token": refresh
        }), 200

    except Exception as e:
        logger.error(f"Email verification failed: {str(e)}")
        return jsonify({"error": "Verification failed"}), 400
    
# POST /api/auth/2fa/setup
@bp.route("/2fa/setup", methods=["POST"])
@jwt_required()
def setup_2fa():
    user_id = get_jwt_identity()

    try:
        # Supabase built-in TOTP support (you need to enable it in dashboard first)
        factor = supabase.auth.mfa.enroll({
            "factor_type": "totp",
            "issuer": "GigConnect",
            "user_id": user_id
        })

        if not factor:
            return jsonify({"error": "Failed to start 2FA setup"}), 500

        # Return QR code data URI or secret for frontend to display
        return jsonify({
            "success": True,
            "qr_code": factor.totp.qr_code,           # base64 data URI
            "secret": factor.totp.secret,             # for manual entry
            "factor_id": factor.id
        }), 200

    except Exception as e:
        logger.error(f"2FA setup failed for {user_id}: {str(e)}")
        return jsonify({"error": "Failed to setup 2FA"}), 500


# POST /api/auth/2fa/verify
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
        verified = supabase.auth.mfa.verify({
            "factor_id": factor_id,
            "code": code
        })

        if not verified:
            return jsonify({"error": "Invalid 2FA code"}), 401

        # Mark 2FA as enabled in profile (optional)
        supabase.table("profiles")\
            .update({"two_factor_enabled": True, "updated_at": "now()"})\
            .eq("id", user_id)\
            .execute()

        log_action(user_id, "2fa_enabled")

        return jsonify({"success": True, "message": "2FA enabled"}), 200

    except Exception as e:
        logger.error(f"2FA verify failed for {user_id}: {str(e)}")
        return jsonify({"error": "Verification failed"}), 400


# POST /api/auth/2fa/disable
@bp.route("/2fa/disable", methods=["POST"])
@jwt_required()
def disable_2fa():
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    code = data.get("code")

    if not code:
        return jsonify({"error": "2FA code required to disable"}), 400

    try:
        # You need to verify current 2FA code before disabling
        # (requires active factor – you can get it from enroll or list)
        factors = supabase.auth.mfa.list_user_factors()
        totp_factor = next((f for f in factors if f.factor_type == "totp"), None)

        if not totp_factor:
            return jsonify({"error": "No 2FA enabled"}), 400

        verified = supabase.auth.mfa.verify({
            "factor_id": totp_factor.id,
            "code": code
        })

        if not verified:
            return jsonify({"error": "Invalid code"}), 401

        supabase.auth.mfa.unenroll(totp_factor.id)

        supabase.table("profiles")\
            .update({"two_factor_enabled": False, "updated_at": "now()"})\
            .eq("id", user_id)\
            .execute()

        log_action(user_id, "2fa_disabled")

        return jsonify({"success": True, "message": "2FA disabled"}), 200

    except Exception as e:
        logger.error(f"2FA disable failed for {user_id}: {str(e)}")
        return jsonify({"error": "Failed to disable 2FA"}), 400
    
# POST /api/auth/reset-password/confirm (optional extra step)
@bp.route("/reset-password/confirm", methods=["POST"])
def reset_password_confirm():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    password = data.get("password")

    if not token or not password:
        return jsonify({"error": "Token and new password required"}), 400

    # Same as your existing reset logic
    try:
        res = supabase.auth.update_user({"password": password})

        if res.user:
            log_action(res.user.id, "password_reset_confirmed")
            return jsonify({"message": "Password reset successful"}), 200
        else:
            return jsonify({"error": "Invalid or expired token"}), 400

    except Exception as e:
        logger.error(f"Reset confirm error: {str(e)}")
        return jsonify({"error": "Failed to reset password"}), 500
    
# ────────────────────────────────────────────────
# POST /api/auth/oauth/<provider> → Generate OAuth URL
# ────────────────────────────────────────────────
@bp.route("/oauth/<provider>", methods=["POST"])
def start_oauth(provider: str):
    if provider not in ["google", "facebook"]:
        logger.warning(f"Unsupported OAuth provider requested: {provider}")
        return jsonify({"error": "Unsupported provider"}), 400

    data = request.get_json(silent=True) or {}
    frontend_redirect = data.get("redirect_to")

    if not frontend_redirect:
        frontend_redirect = f"{request.host_url.rstrip('/')}/oauth/callback"
        logger.info(f"No redirect_to provided - using default: {frontend_redirect}")

    try:
        oauth_response = supabase.auth.sign_in_with_oauth({
            "provider": provider,
            "options": {
                "redirect_to": frontend_redirect,
                "scopes": "email profile" if provider == "google" else "email public_profile"
            }
        })

        oauth_url = oauth_response.url

        if not oauth_url:
            logger.error(f"No OAuth URL returned for {provider}")
            return jsonify({"error": "Failed to generate OAuth URL - no URL in response"}), 500

        logger.info(f"Generated {provider} OAuth URL: {oauth_url}")

        return jsonify({
            "success": True,
            "oauth_url": oauth_url,
            "provider": provider,
            "redirect_to": frontend_redirect
        }), 200

    except AttributeError as ae:
        logger.error(f"Supabase attribute error: {str(ae)} - Check SDK version or response structure")
        return jsonify({
            "error": "Internal server error - OAuth method issue",
            "details": "Check supabase-py version and response format"
        }), 500
    except Exception as e:
        logger.exception(f"Failed to generate {provider} OAuth URL")
        return jsonify({
            "error": "Failed to generate OAuth URL",
            "details": str(e)
        }), 500


# ────────────────────────────────────────────────
# POST /api/auth/oauth/callback → Exchange code for tokens
# ────────────────────────────────────────────────
@bp.route("/oauth/callback", methods=["POST"])
def oauth_callback():
    data = request.get_json(silent=True) or {}
    code = data.get("code")
    provider = data.get("provider")

    if not code or not provider:
        logger.warning("Missing code or provider in OAuth callback")
        return jsonify({"error": "Authorization code and provider required"}), 400

    try:
        session = supabase.auth.exchange_code_for_session(code)

        if not session or not session.user:
            logger.error(f"No session/user returned for {provider} OAuth")
            return jsonify({"error": "OAuth authentication failed - no user session"}), 401

        user = session.user

        profile_res = supabase.table("profiles")\
            .select("*")\
            .eq("id", user.id)\
            .maybe_single().execute()

        profile = profile_res.data

        if not profile:
            full_name = (
                user.user_metadata.get("full_name") or
                user.user_metadata.get("name") or
                user.email.split("@")[0].title()
            )

            profile_data = {
                "id": user.id,
                "email": user.email,
                "full_name": full_name,
                "avatar_url": user.user_metadata.get("avatar_url"),
                "role": "buyer",
                "created_at": "now()",
                "updated_at": "now()"
            }

            insert_res = supabase.table("profiles").insert(profile_data).execute()

            if not insert_res.data:
                logger.error(f"Failed to create profile for OAuth user {user.id}")
                return jsonify({"error": "Failed to create user profile"}), 500

            profile = profile_data

        access_token = create_access_token(identity=user.id)
        refresh_token = create_refresh_token(identity=user.id)

        logger.info(f"Successful {provider} OAuth login | user_id={user.id} | email={user.email}")

        return jsonify({
            "success": True,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user": {
                "id": user.id,
                "email": user.email,
                "full_name": profile["full_name"],
                "avatar_url": profile["avatar_url"],
                "role": profile["role"]
            }
        }), 200

    except Exception as e:
        logger.exception(f"OAuth callback failed for {provider}")
        return jsonify({
            "error": "Failed to complete OAuth login",
            "details": str(e)
        }), 500
    
@bp.route("/test-supabase-login", methods=["POST"])
def test_supabase_login():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")
    try:
        res = supabase.auth.sign_in_with_password({"email": email, "password": password})
        return jsonify({
            "success": True,
            "user_id": res.user.id if res.user else None,
            "session": res.session is not None
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500