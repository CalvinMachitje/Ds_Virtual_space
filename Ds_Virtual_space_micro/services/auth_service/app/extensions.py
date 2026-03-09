# services/auth_service/app/extensions.py
import json, time, logging, threading, os, redis
from typing import Optional, Any
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
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ────────────────────────────────
# Extensions
# ────────────────────────────────
socketio = SocketIO(
    logger=True,
    engineio_logger=True,
    ping_timeout=60,
    ping_interval=25,
    async_mode="eventlet",
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

# ────────────────────────────────
# Redis
# ────────────────────────────────
redis_client: Optional[redis.Redis] = None

def init_redis(app: Flask) -> None:
    global redis_client
    if redis_client is not None:
        logger.debug("Redis already initialized")
        return

    redis_url = app.config.get("REDIS_URL") or os.getenv("REDIS_URL") or "redis://localhost:6379/0"
    max_retries, retry_delay = 5, 2

    for attempt in range(max_retries):
        try:
            redis_client = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5
            )
            if redis_client.ping():
                logger.info(f"Redis connected successfully: {redis_url}")
                return
        except Exception as e:
            logger.warning(f"Redis connection attempt {attempt+1} failed: {e}")
            time.sleep(retry_delay)

    logger.critical(f"Redis connection FAILED after {max_retries} attempts")
    redis_client = None

# ────────────────────────────────
# Safe Redis wrapper
# ────────────────────────────────
def safe_redis_call(method_name: str, *args, default: Any = None) -> Any:
    global redis_client
    if redis_client is None:
        return default
    try:
        method = getattr(redis_client, method_name)
        return method(*args)
    except Exception:
        return default

# ────────────────────────────────
# Initialize all extensions
# ────────────────────────────────
def init_extensions(app: Flask) -> None:
    global redis_client

    # Redis
    init_redis(app)

    # Socket.IO with Redis pub/sub if available
    socketio_message_queue = app.config.get("REDIS_URL") if redis_client else None
    socketio.init_app(
        app,
        cors_allowed_origins=app.config.get("FRONTEND_ORIGINS", ["*"]),
        message_queue=socketio_message_queue,
        logger=True,
        engineio_logger=True
    )
    if redis_client:
        logger.info("Socket.IO message queue set to Redis")

    # Other extensions
    cors.init_app(
        app,
        resources={
            r"/api/*": {
                "origins": app.config.get("FRONTEND_ORIGINS", ["http://localhost:5173"]),
                "supports_credentials": True,
                "allow_headers": ["Content-Type", "Authorization", "X-Requested-With"],
                "expose_headers": ["Content-Type", "Authorization"],
                "max_age": 86400
            },
            r"/socket.io/*": {
                "origins": app.config.get("FRONTEND_ORIGINS", ["http://localhost:5173"]),
                "supports_credentials": True
            }
        }
    )
    jwt.init_app(app)
    limiter.init_app(app)
    mail.init_app(app)
    cache.init_app(app)
    compress.init_app(app)
    talisman.init_app(app, force_https=not app.debug)

    # Start Redis log pub/sub listener
    if redis_client and os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_redis_log_listener(app)

    logger.info("All extensions initialized successfully")

# ────────────────────────────────
# Logging setup
# ────────────────────────────────
def setup_logging(app: Flask) -> None:
    log_level = logging.DEBUG if app.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler()],
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("socketio").setLevel(logging.WARNING)
    logging.getLogger("engineio").setLevel(logging.WARNING)
    logger.info(f"Logging configured at level: {logging.getLevelName(log_level)}")

REDIS_LOG_LIST = "live_logs"
REDIS_LOG_MAX = 1000
REDIS_LOG_CHANNEL = "live_logs_channel"

def start_redis_log_listener(app: Flask):
    """
    Background Redis pub/sub listener.
    Uses dedicated Redis connection without socket timeout.
    Auto-recovers on failure.
    """

    redis_url = app.config.get("REDIS_URL")

    if not redis_url:
        logger.warning("No REDIS_URL configured. Pub/sub disabled.")
        return

    def listener():
        while True:
            try:
                # Dedicated connection for pub/sub
                pubsub_redis = redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_timeout=None,  # ← IMPORTANT FIX
                    socket_connect_timeout=5,
                )

                pubsub = pubsub_redis.pubsub()
                pubsub.subscribe(REDIS_LOG_CHANNEL)

                logger.info("Subscribed to Redis live_logs_channel")

                for message in pubsub.listen():
                    if message["type"] != "message":
                        continue

                    try:
                        log_data = json.loads(message["data"])
                        socketio.emit("new_log", log_data, namespace="/")
                    except Exception as e:
                        logger.error(f"Emit error: {e}")

            except Exception as e:
                logger.error(f"Redis pub/sub error: {e}")
                logger.info("Reconnecting pub/sub in 3 seconds...")
                time.sleep(3)

    thread = threading.Thread(target=listener, daemon=True)
    thread.start()