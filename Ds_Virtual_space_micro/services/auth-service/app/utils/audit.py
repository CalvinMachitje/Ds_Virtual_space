# services/auth-service/app/utils/audit.py
import logging
from datetime import datetime

from .redis_utils import safe_redis_call


logger = logging.getLogger(__name__)

def log_action(actor_id: str | None, action: str, details: dict | None = None, ip: str | None = None):
    if actor_id is None:
        actor_id = "anonymous"

    if ip is None:
        ip = "unknown"  # replace with request.remote_addr if you have access

    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "actor_id": actor_id,
        "action": action,
        "ip": ip,
        "details": details or {},
    }

    logger.info(f"AUDIT | {action} | {entry}")

    try:
        if safe_redis_call("exists", "audit:log"):  # dummy check
            safe_redis_call("lpush", "audit:log", str(entry))
            safe_redis_call("ltrim", "audit:log", 0, 9999)
    except Exception as e:
        logger.warning(f"Audit Redis write failed: {e}")