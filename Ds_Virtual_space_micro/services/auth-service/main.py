# services/auth-service/main.py (updated – replace completely)

import os
import logging
from flask import Flask, jsonify
from dotenv import load_dotenv
from flask_cors import CORS
from datetime import datetime, timezone, timedelta

from app.extensions.extensions import (
    redis_client,
    init_redis,
    init_extensions,
    setup_logging,
    socketio,
    jwt,
    limiter,
    cors,
    mail,
    cache,
    compress,
    talisman,
)

load_dotenv()

logger = logging.getLogger(__name__)

def create_app():
    app = Flask(__name__, instance_relative_config=False)

    # ────────────────────────────────────────────────
    # Banner / Startup Info
    # ────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("Starting Auth Microservice")
    logger.info(f"Environment: {'DEBUG' if app.debug else 'PRODUCTION'}")
    logger.info(f"REDIS_URL: {os.getenv('REDIS_URL', 'not set')}")
    logger.info("=" * 60)

    # ────────────────────────────────────────────────
    # Configuration
    # ────────────────────────────────────────────────
    jwt_secret = os.getenv("JWT_SECRET_KEY")
    if not jwt_secret or jwt_secret.strip() == "":
        raise RuntimeError("JWT_SECRET_KEY is missing or empty in .env")

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        logger.warning("REDIS_URL not set - falling back to localhost")
        redis_url = "redis://localhost:6379/0"

    access_expires_min = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRES_MINUTES", "10080"))
    refresh_expires_days = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRES_DAYS", "30"))

    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY") or os.urandom(32).hex(),
        JWT_SECRET_KEY=jwt_secret,
        JWT_ACCESS_TOKEN_EXPIRES=timedelta(minutes=access_expires_min),
        JWT_REFRESH_TOKEN_EXPIRES=timedelta(days=refresh_expires_days),
        REDIS_URL=redis_url,
        SUPABASE_URL=os.getenv("SUPABASE_URL"),
        SUPABASE_KEY=os.getenv("SUPABASE_KEY"),
        DEBUG=os.getenv("FLASK_DEBUG", "False").lower() == "true",
        JWT_VERIFY_EXPIRATION=True,
        JWT_TOKEN_LOCATION=["headers"],
        JWT_COOKIE_SECURE=not app.debug,
        JWT_COOKIE_SAMESITE="Lax",
    )

    # ────────────────────────────────────────────────
    # Logging (early)
    # ────────────────────────────────────────────────
    setup_logging(app)

    # ────────────────────────────────────────────────
    # IMPORTANT: Only initialize extensions in the main process
    # (prevents duplicate init in reloader child)
    # ────────────────────────────────────────────────
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        logger.info("Running in main process – initializing extensions")
        init_redis(app)
        init_extensions(app)
    else:
        logger.info("Running in reloader child process – skipping extensions init")

    # ────────────────────────────────────────────────
    # CORS
    # ────────────────────────────────────────────────
    CORS(
        app,
        resources={
            r"/api/*": {"origins": "*"},
            r"/socket.io/*": {"origins": "*"}
        },
        supports_credentials=True
    )

    # ────────────────────────────────────────────────
    # Register blueprint
    # ────────────────────────────────────────────────
    from app.routes.auth import bp as auth_bp
    app.register_blueprint(auth_bp)

    # ────────────────────────────────────────────────
    # Health check
    # ────────────────────────────────────────────────
    @app.route("/api/health")
    def health():
        redis_ok = bool(redis_client and redis_client.ping())
        return jsonify({
            "status": "healthy" if redis_ok else "degraded",
            "redis": "connected" if redis_ok else "failed",
            "service": "auth-service",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "debug": {
                "redis_client_exists": redis_client is not None,
                "debug_mode": app.debug
            }
        }), 200 if redis_ok else 503

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", 5001))
    logger.info(f"Starting server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=app.config["DEBUG"])