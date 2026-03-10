# services/auth-service/main.py
import datetime
import os
from flask import Flask
from dotenv import load_dotenv
from flask_cors import CORS
from datetime import timedelta
from app.extensions import redis_client

load_dotenv()

def create_app():
    app = Flask(__name__, instance_relative_config=False)

    # ────────────────────────────────────────────────
    # Configuration (load from .env, fallback to safe defaults)
    # ────────────────────────────────────────────────
    jwt_secret = os.getenv("JWT_SECRET_KEY")
    if not jwt_secret or jwt_secret.strip() == "":
        raise RuntimeError("JWT_SECRET_KEY is missing or empty in .env")

    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY") or os.urandom(32).hex(),
        JWT_SECRET_KEY=jwt_secret,
        JWT_ACCESS_TOKEN_EXPIRES=timedelta(minutes=int(os.getenv("JWT_ACCESS_TOKEN_EXPIRES_MINUTES", "10080"))),
        JWT_REFRESH_TOKEN_EXPIRES=timedelta(days=int(os.getenv("JWT_REFRESH_TOKEN_EXPIRES_DAYS", "30"))),
        REDIS_URL=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        SUPABASE_URL=os.getenv("SUPABASE_URL"),
        SUPABASE_KEY=os.getenv("SUPABASE_KEY"),
        DEBUG=os.getenv("FLASK_DEBUG", "False").lower() == "true",
        JWT_VERIFY_EXPIRATION=True,
        JWT_TOKEN_LOCATION=["headers"],
        JWT_COOKIE_SECURE=False,           # False in dev, True in prod
        JWT_COOKIE_SAMESITE="Lax",
    )

    # CORS – restrict in production
    CORS(app, resources={r"/api/*": {"origins": ["http://localhost:5173", "*"]}})

    # ────────────────────────────────────────────────
    # Initialize extensions (Redis, etc.)
    # ────────────────────────────────────────────────
    from app.extensions import init_redis
    init_redis(app)

    # ────────────────────────────────────────────────
    # Register auth blueprint
    # ────────────────────────────────────────────────
    from app.routes.auth import bp as auth_bp
    app.register_blueprint(auth_bp)

    # Health check endpoint
    @app.route("/api/health")
    def health():
        redis_ok = redis_client.ping() if redis_client else False
        return {
            "status": "healthy" if redis_ok else "degraded",
            "redis": "connected" if redis_ok else "failed",
            "service": "auth-service",
            "timestamp": datetime.utcnow().isoformat()
        }, 200 if redis_ok else 503

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=app.config["DEBUG"])