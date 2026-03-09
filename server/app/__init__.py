# server/app/__init__.py
import os
import uuid
import logging
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, g
from flask_jwt_extended import get_jwt, verify_jwt_in_request
from werkzeug.exceptions import HTTPException
from dotenv import load_dotenv

from app.extensions import (
    socketio,
    jwt,
    limiter,
    cors,
    migrate,
    mail,
    cache,
    compress,
    talisman,
    init_extensions,
    setup_logging,
    redis_client,
)

load_dotenv()
logger = logging.getLogger(__name__)

def create_app() -> Flask:
    app = Flask(__name__)

    # ────────────────────────────────
    # 🔐 Configuration
    # ────────────────────────────────
    jwt_secret = os.getenv("JWT_SECRET_KEY")
    if not jwt_secret:
        raise RuntimeError("JWT_SECRET_KEY not set")

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL must be set")

    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY") or os.urandom(32).hex(),
        JWT_SECRET_KEY=jwt_secret,
        JWT_ACCESS_TOKEN_EXPIRES=timedelta(minutes=45),
        JWT_REFRESH_TOKEN_EXPIRES=timedelta(days=7),
        JWT_TOKEN_LOCATION=["headers"],
        JWT_COOKIE_SECURE=True,
        JWT_COOKIE_SAMESITE="Strict",
        JWT_COOKIE_CSRF_PROTECT=True,
        REDIS_URL=redis_url,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=1),
        PROPAGATE_EXCEPTIONS=False,
    )

    # ────────────────────────────────
    # 📜 Logging
    # ────────────────────────────────
    setup_logging(app)

    # ────────────────────────────────
    # 🌍 Frontend origins
    # ────────────────────────────────
    frontend_origins = os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:5173,http://196.253.26.122:5173"
    ).split(",")
    app.config["FRONTEND_ORIGINS"] = frontend_origins

    # ────────────────────────────────
    # 🧠 Initialize extensions
    # ────────────────────────────────
    init_extensions(app)

    # ────────────────────────────────
    # 🔒 JWT Blocklist
    # ────────────────────────────────
    @jwt.token_in_blocklist_loader
    def check_if_token_revoked(jwt_header, jwt_payload):
        jti = jwt_payload.get("jti")
        return redis_client.get(f"blacklist:{jti}") == "true" if redis_client else False

    # ────────────────────────────────
    # 📦 Register Blueprints
    # ────────────────────────────────
    from app.routes.auth import bp as auth_bp
    from app.routes.admin import bp as admin_bp
    from app.routes.buyer import bp as buyer_bp
    from app.routes.seller import bp as seller_bp
    from app.routes.shared import bp as shared_bp
    from app.routes.support import bp as support_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(buyer_bp)
    app.register_blueprint(seller_bp)
    app.register_blueprint(shared_bp)
    app.register_blueprint(support_bp)

    # ────────────────────────────────
    # 🆔 Request ID Middleware
    # ────────────────────────────────
    @app.before_request
    def handle_options():
        if request.method == "OPTIONS":
            # Return 204 immediately — do NOT let any other before_request run
            response = app.make_response(('', 204))
            
            # Very permissive for dev (tighten later)
            origin = request.headers.get("Origin")
            if origin in app.config["FRONTEND_ORIGINS"] or "*" in app.config["FRONTEND_ORIGINS"]:
                response.headers["Access-Control-Allow-Origin"] = origin or "*"
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Accept"
                response.headers["Access-Control-Max-Age"] = "86400"  # cache preflight 24h
            return response

    @app.after_request
    def add_cors_headers(response):
        # Ensure credentials header is always set for CORS requests
        if 'Access-Control-Allow-Origin' in response.headers:
            response.headers['Access-Control-Allow-Credentials'] = 'true'
        
        # Also fix for error responses (Flask doesn't always apply CORS to them)
        origin = request.headers.get('Origin')
        if origin and origin in app.config.get('FRONTEND_ORIGINS', []):
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Credentials'] = 'true'
        
        return response

    # ────────────────────────────────
    # ⚡ Preflight OPTIONS handler for React / API CORS
    # ────────────────────────────────
    @app.before_request
    def handle_options_preflight():
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            response = app.make_response(('', 204))
            response.headers["Access-Control-Allow-Origin"] = ",".join(app.config.get("FRONTEND_ORIGINS", ["*"]))
            response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization,X-Requested-With"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            return response

    # ────────────────────────────────
    # ❤️ Health Check
    # ────────────────────────────────
    @app.route("/api/health")
    def health():
        redis_status = "ok" if redis_client and redis_client.ping() else "failed"
        return jsonify({
            "status": "ok",
            "redis": redis_status,
            "timestamp": datetime.utcnow().isoformat(),
        })

    # ────────────────────────────────
    # 🛑 Global Error Handler
    # ────────────────────────────────
    @app.errorhandler(Exception)
    def handle_exception(e):
        if isinstance(e, HTTPException):
            return jsonify({"error": e.description}), e.code
        logger.exception(f"[{getattr(g, 'request_id', 'unknown')}] Unhandled exception")
        return jsonify({"error": "Internal server error"}), 500

    # ────────────────────────────────
    # ⚡ Socket.IO JWT Admin Auth
    # ────────────────────────────────
    @socketio.on("connect")
    def socket_connect(auth):
        token = auth.get("token") if auth else None
        from flask_jwt_extended import decode_token
        try:
            decoded = decode_token(token) if token else None
        except Exception:
            decoded = None
        if not decoded or decoded.get("role") != "admin":
            print("Socket rejected: invalid token")
            return False  # disconnect
        print(f"Socket connected: {decoded['sub']}")

    @socketio.on("subscribe_logs")
    def handle_subscribe_logs():
        print("Admin subscribed to live logs")

    @socketio.on("unsubscribe_logs")
    def handle_unsubscribe_logs():
        print("Admin unsubscribed from live logs")

    return app