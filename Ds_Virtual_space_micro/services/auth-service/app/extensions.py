
# services/auth-service/app/extensions.py
"""
Centralized Flask extensions for auth-service microservice.
All extensions are created here and initialized via init_extensions().
"""

import os
import json
import time
import logging
import threading
from typing import Optional, Any

import redis
from flask import Flask
from flask_socketio import SocketIO
from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_cors import CORS
from flask_mail import Mail
from flask_caching import Cache
from flask_compress import Compress
from flask_talisman import Talisman

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────
# Core Extensions
# ────────────────────────────────────────────────

socketio = SocketIO(
    cors_allowed_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://196.253.26.113:5173",
        "http://196.253.26.113",
        "*"
    ],
    async_mode="eventlet",
    ping_timeout=60,
    ping_interval=25,
    logger=True,
    engineio_logger=True,
    cors_credentials=True,
    cors_methods=["GET", "POST", "OPTIONS"],
    cors_headers=["Content-Type", "Authorization", "X-Requested-With"]
)

jwt = JWTManager()
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.getenv("REDIS_URL") or "redis://localhost:6379/0"
)
cors = CORS()
migrate = Migrate()
mail = Mail()
cache = Cache(config={"CACHE_TYPE": "redis", "CACHE_DEFAULT_TIMEOUT": 300})
compress = Compress()
talisman = Talisman()

# ────────────────────────────────────────────────
# Redis client
# ────────────────────────────────────────────────

redis_client: Optional[redis.Redis] = None

def init_redis(app: Flask) -> None:
    """Initialize Redis with retries and logging."""
    global redis_client
    if redis_client:
        logger.debug("Redis already initialized")
        return

    redis_url = app.config.get("REDIS_URL") or os.getenv("REDIS_URL") or "redis://localhost:6379/0"
    max_retries = 5
    retry_delay = 2

    for attempt in range(1, max_retries + 1):
        try:
            redis_client = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
                retry_on_timeout=True,
                max_connections=20
            )
            pong = redis_client.ping()
            if pong is True or pong in (b"PONG", "PONG"):
                logger.info(f"Redis connected successfully: {redis_url}")
                logger.debug(f"Server info: {redis_client.info('server')}")
                return
            else:
                logger.warning(f"Unexpected PING result: {pong}")
        except redis.ConnectionError as e:
            logger.warning(f"ConnectionError on attempt {attempt}: {str(e)}")
        except redis.TimeoutError as e:
            logger.warning(f"TimeoutError on attempt {attempt}: {str(e)}")
        except redis.RedisError as e:
            logger.warning(f"RedisError on attempt {attempt}: {str(e)}")
        except Exception as e:
            logger.exception(f"Unexpected error on attempt {attempt}: {str(e)}")

        if attempt < max_retries:
            logger.debug(f"Retrying in {retry_delay}s...")
            time.sleep(retry_delay)

    logger.critical(f"Redis connection FAILED after {max_retries} attempts")
    redis_client = None

def safe_redis_call(method_name: str, *args, default: Any = None) -> Any:
    """Call a Redis method safely; returns default on failure."""
    if not redis_client:
        logger.warning(f"Redis unavailable - skipping {method_name}")
        return default
    try:
        method = getattr(redis_client, method_name)
        result = method(*args)
        logger.debug(f"Redis {method_name} succeeded")
        return result
    except Exception as e:
        logger.error(f"Redis {method_name} failed: {str(e)}")
        return default

# ────────────────────────────────────────────────
# Initialize all extensions
# ────────────────────────────────────────────────

def init_extensions(app: Flask) -> None:
    """Initialize extensions in correct order."""
    init_redis(app)

    if redis_client:
        socketio.message_queue = "redis://"
        logger.info("Socket.IO message queue set to Redis")
    else:
        logger.warning("Redis unavailable – Socket.IO pub/sub disabled")
        socketio.message_queue = None

    socketio.init_app(app)
    cors.init_app(
        app,
        resources={
            r"/api/*": {"origins": "*"},
            r"/socket.io/*": {"origins": "*"}
        },
        supports_credentials=True
    )
    jwt.init_app(app)
    limiter.init_app(app)
    mail.init_app(app)
    cache.init_app(app)
    compress.init_app(app)
    talisman.init_app(app, force_https=not app.debug)
    migrate.init_app(app)

    logger.info("All extensions initialized successfully")

# ────────────────────────────────────────────────
# Logging setup
# ────────────────────────────────────────────────

def setup_logging(app: Flask) -> None:
    log_level = logging.DEBUG if app.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler()]
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("socketio").setLevel(logging.WARNING)
    logging.getLogger("engineio").setLevel(logging.WARNING)
    logger.info(f"Logging configured at level: {logging.getLevelName(log_level)}")

# ────────────────────────────────────────────────
# Optional Redis → Socket.IO log listener
# ────────────────────────────────────────────────

REDIS_LOG_CHANNEL = "live_logs_channel"

def start_redis_log_listener(app: Flask):
    """Background thread: Redis pub/sub → Socket.IO broadcast."""
    if not redis_client:
        logger.warning("No Redis client – live logs pub/sub disabled")
        return

    def listener():
        while True:
            try:
                pubsub = redis_client.pubsub()
                pubsub.subscribe(REDIS_LOG_CHANNEL)
                logger.info(f"Subscribed to {REDIS_LOG_CHANNEL}")
                for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    try:
                        log_data = json.loads(message["data"])
                        socketio.emit("new_log", log_data, namespace="/")
                        logger.debug("Broadcasted live log")
                    except json.JSONDecodeError:
                        logger.error("Invalid JSON in log message")
                    except Exception as e:
                        logger.error(f"Emit failed: {str(e)}")
            except redis.ConnectionError as e:
                logger.error(f"Redis pub/sub lost: {str(e)}")
            except Exception as e:
                logger.error(f"Pub/sub crashed: {str(e)}")
            time.sleep(5)

    thread = threading.Thread(target=listener, daemon=True, name="RedisLogListener")
    thread.start()
    logger.info("Started Redis live logs listener")