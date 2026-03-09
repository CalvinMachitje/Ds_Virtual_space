# services/auth_service/app/utils/audit.py
"""
Audit logging helper - logs actions to console + Redis list.
"""

import logging
from datetime import datetime
from flask import request, current_app
from ..extensions import redis_client

logger = logging.getLogger(__name__)

def log_action(actor_id: str | None, action: str, details: dict | None = None, ip: str | None = None):
    """
    Log an audit action (login, gig create, hire request, etc.).
    Safe even if Redis is down.
    """
    if actor_id is None:
        actor_id = "anonymous"

    if ip is None:
        ip = request.remote_addr if request else "unknown"

    timestamp = datetime.utcnow().isoformat()
    entry = {
        "timestamp": timestamp,
        "actor_id": actor_id,
        "action": action,
        "ip": ip,
        "details": details or {},
        "user_agent": request.user_agent.string if request else "unknown"
    }

    # Always log to console
    logger.info(f"AUDIT [{action}] {entry}")

    # Try to store in Redis (list for easy querying)
    try:
        if redis_client:
            redis_client.lpush("audit:log", str(entry))  # or use JSON.dumps(entry)
            redis_client.ltrim("audit:log", 0, 9999)     # keep last 10k
    except Exception as e:
        logger.warning(f"Audit log to Redis failed: {str(e)}")