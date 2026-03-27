# Audit logging utility for the Admin Service, allowing actions to be logged with details and stored in Redis.
# services/admin-service/app/utils/audit.py
import logging
from datetime import datetime
from app.utils.redis_utils import redis_client

logger = logging.getLogger(__name__)

def log_action(actor_id: str | None, action: str, details: dict | None = None, ip: str | None = None):
    if actor_id is None:
        actor_id = "anonymous"

    timestamp = datetime.utcnow().isoformat()
    entry = {
        "timestamp": timestamp,
        "actor_id": actor_id,
        "action": action,
        "details": details or {},
        "ip": ip or "unknown"
    }

    logger.info(f"AUDIT [{action}] {entry}")

    try:
        if redis_client:
            redis_client.lpush("audit:log", str(entry))
            redis_client.ltrim("audit:log", 0, 9999)
    except Exception as e:
        logger.warning(f"Audit log to Redis failed: {str(e)}")