# services/auth-service/app/__init__.py
import os
import logging
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request, g
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
from dotenv import load_dotenv
from flask_jwt_extended import get_jwt

# ─────────────────────────────────────
# Load environment variables
# ─────────────────────────────────────
load_dotenv()

# ─────────────────────────────────────
# Extensions
# ─────────────────────────────────────
from app.extensions import (
    jwt,
    init_extensions,
    setup_logging,
    redis_client,
    init_redis
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────
# Application Factory
# ─────────────────────────────────────
def create_app() -> Flask:
    app = Flask(__name__)

    # ─────────────────────────────────────
    # Configuration
    # ─────────────────────────────────────
    jwt_secret = os.getenv("JWT_SECRET_KEY")
    if not jwt_secret or jwt_secret.strip() == "":
        raise RuntimeError("JWT_SECRET_KEY is missing or empty in .env")

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    access_expires_min = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRES_MINUTES", "10080"))
    refresh_expires_days = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRES_DAYS", "30"))

    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY") or os.urandom(32).hex(),
        JWT_SECRET_KEY=jwt_secret,
        JWT_ACCESS_TOKEN_EXPIRES=timedelta(minutes=access_expires_min),
        JWT_REFRESH_TOKEN_EXPIRES=timedelta(days=refresh_expires_days),
        JWT_TOKEN_LOCATION=["headers"],
        JWT_VERIFY_EXPIRATION=True,
        JWT_COOKIE_SECURE=False,  # True in production
        JWT_COOKIE_SAMESITE="Lax",
        REDIS_URL=redis_url,
        DEBUG=os.getenv("FLASK_DEBUG", "False").lower() == "true",
        SUPABASE_URL=os.getenv("SUPABASE_URL"),
        SUPABASE_KEY=os.getenv("SUPABASE_KEY"),
    )

    # ─────────────────────────────────────
    # Logging
    # ─────────────────────────────────────
    setup_logging(app)

    # ─────────────────────────────────────
    # Initialize extensions
    # ─────────────────────────────────────
    init_extensions(app)
    init_redis(app)

    # ─────────────────────────────────────
    # CORS
    # ─────────────────────────────────────
    frontend_origins = os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:5173,http://196.253.26.122:5173"
    ).split(",")

    allowed_origins = [o.strip() for o in frontend_origins if o.strip()]

    CORS(
        app,
        resources={r"/api/*": {"origins": allowed_origins}},
        supports_credentials=True
    )
    app.config["FRONTEND_ORIGINS"] = allowed_origins

    # ─────────────────────────────────────
    # JWT Token Blocklist (Redis)
    # ─────────────────────────────────────
    @jwt.token_in_blocklist_loader
    def check_if_token_revoked(jwt_header, jwt_payload):
        jti = jwt_payload.get("jti")
        if not redis_client:
            return False
        token = redis_client.get(f"blacklist:{jti}")
        return token == "true"

    # ─────────────────────────────────────
    # Blueprints
    # ─────────────────────────────────────
    from app.routes.auth.routes import bp as routes_bp
    from app.routes.auth.oauth import bp as oauth_bp
    from app.routes.auth.twofa import bp as twofa_bp
    from app.routes.auth.admin import bp as admin_bp

    # Mount all auth routes under /api/auth
    app.register_blueprint(routes_bp, url_prefix="/api/auth")
    app.register_blueprint(oauth_bp, url_prefix="/api/auth")
    app.register_blueprint(twofa_bp, url_prefix="/api/auth")
    app.register_blueprint(admin_bp, url_prefix="/api/auth")

    logger.info("All auth blueprints initialized")

    # ─────────────────────────────────────
    # Global CORS preflight handler
    # ─────────────────────────────────────
    @app.before_request
    def handle_options():
        if request.method == "OPTIONS":
            response = app.make_response(('', 204))
            origin = request.headers.get("Origin")
            allowed = app.config["FRONTEND_ORIGINS"]
            if origin in allowed or "*" in allowed:
                response.headers["Access-Control-Allow-Origin"] = origin or "*"
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
                response.headers["Access-Control-Max-Age"] = "86400"
            return response

    @app.after_request
    def add_cors_headers(response):
        origin = request.headers.get("Origin")
        allowed = app.config["FRONTEND_ORIGINS"]
        if origin in allowed or "*" in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    # ─────────────────────────────────────
    # Health check endpoint
    # ─────────────────────────────────────
    @app.route("/api/health")
    def health():
        redis_status = "failed"
        if redis_client:
            try:
                redis_client.ping()
                redis_status = "ok"
            except Exception:
                redis_status = "failed"
        return jsonify({
            "status": "ok" if redis_status == "ok" else "degraded",
            "service": "auth-service",
            "redis": redis_status,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    # ─────────────────────────────────────
    # Global error handler
    # ─────────────────────────────────────
    @app.errorhandler(Exception)
    def handle_exception(e):
        if isinstance(e, HTTPException):
            return jsonify({"error": e.description}), e.code
        logger.exception(f"[{getattr(g, 'request_id', 'unknown')}] Unhandled exception")
        return jsonify({"error": "Internal server error"}), 500

    return app
