import json
from typing import Any, Dict
from datetime import datetime
from app.extensions import REDIS_LOG_CHANNEL, REDIS_LOG_CHANNEL, redis_client, socketio

REDIS_LOG_LIST = "live_logs"   # Redis list key for recent logs
REDIS_LOG_MAX = 1000           # Keep last 1000 logs only

def broadcast_log(log: Dict[str, Any]) -> None:
    """
    Multi-worker safe log broadcast.

    1. Stores log in Redis list
    2. Publishes log to Redis channel
    3. All workers receive via pub/sub and emit to Socket.IO
    """

    if "created_at" not in log:
        log["created_at"] = datetime.utcnow().isoformat()

    log_json = json.dumps(log)

    if redis_client:
        try:
            # Store recent logs
            redis_client.lpush(REDIS_LOG_LIST, log_json)
            redis_client.ltrim(REDIS_LOG_LIST, 0, REDIS_LOG_MAX - 1)

            # Publish to channel for all workers
            redis_client.publish(REDIS_LOG_CHANNEL, log_json)

        except Exception as e:
            print(f"Redis broadcast failed: {e}")