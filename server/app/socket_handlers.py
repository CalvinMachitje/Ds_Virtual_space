# server/app/socket_handlers.py
from flask import request
from flask_socketio import emit, join_room, leave_room, disconnect
from flask_jwt_extended import decode_token
from app.extensions import safe_redis_call, socketio, redis_client
from app.services.supabase_service import supabase
from datetime import datetime
import logging
import time

logger = logging.getLogger(__name__)

# Rate limit settings - 8 messages per 60 seconds per user
RATE_LIMIT_COUNT = 8
RATE_LIMIT_WINDOW_SECONDS = 60

def is_rate_limited(user_id: str) -> bool:
    """Check if user exceeded message rate limit using Redis sorted set."""
    if redis_client is None:
        logger.warning("Redis unavailable - rate limiting skipped")
        return False

    key = f"chat_rate_limit:{user_id}"
    now = time.time()

    safe_redis_call("zremrangebyscore", key, 0, now - RATE_LIMIT_WINDOW_SECONDS)
    count = safe_redis_call("zcard", key) or 0

    if count >= RATE_LIMIT_COUNT:
        return True

    safe_redis_call("zadd", key, {str(now): now})
    safe_redis_call("expire", key, RATE_LIMIT_WINDOW_SECONDS + 10)

    return False


@socketio.on("connect")
def handle_connect():
    """Authenticate Socket.IO connection via JWT token from query or header."""
    # Prefer Authorization header, fallback to ?token=
    token = request.headers.get("Authorization", "").replace("Bearer ", "") or request.args.get("token")

    if not token:
        logger.warning("Socket connect attempt without token")
        emit("error", {"message": "Authentication token required"})
        return disconnect()

    try:
        decoded = decode_token(token)
        user_id = decoded.get("sub")  # JWT subject = user ID

        if not user_id:
            raise ValueError("No user ID in token")

        # Join private user room for targeted notifications
        join_room(f"user_{user_id}")

        emit("connected", {
            "message": "Real-time connection established",
            "user_id": user_id
        })

        logger.info(f"Socket authenticated: user {user_id} from {request.remote_addr}")

    except Exception as e:
        logger.error(f"Socket auth failed: {str(e)}", exc_info=True)
        emit("error", {"message": "Invalid or expired token"})
        return disconnect()


@socketio.on("disconnect")
def handle_disconnect():
    logger.info(f"Socket client disconnected: {request.remote_addr}")


@socketio.on("join_conversation")
def on_join_conversation(data):
    """Join a conversation room (e.g. booking chat)."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "") or request.args.get("token")
    if not token:
        emit("error", {"message": "Token required"})
        return

    try:
        decoded = decode_token(token)
        user_id = decoded["sub"]
    except Exception as e:
        logger.error(f"Join conversation - invalid token: {str(e)}")
        emit("error", {"message": "Invalid authentication"})
        return

    conversation_id = data.get("conversation_id")
    if not conversation_id:
        emit("error", {"message": "conversation_id required"})
        return

    room = f"conv_{conversation_id}"
    join_room(room)

    emit("status", {
        "message": f"Joined conversation {conversation_id}",
        "user_id": user_id
    }, room=request.sid)

    # Mark unread messages as read
    try:
        unread = supabase.table("messages")\
            .update({"read_at": datetime.utcnow().isoformat()})\
            .eq("receiver_id", user_id)\
            .eq("booking_id", conversation_id)\
            .is_("read_at", None)\
            .execute()

        if unread.data:
            emit("messages_read", {
                "booking_id": conversation_id,
                "count": len(unread.data)
            }, room=room)
    except Exception as e:
        logger.error(f"Failed to mark messages as read: {str(e)}", exc_info=True)


@socketio.on("send_message")
def on_send_message(data):
    """Handle sending chat message with rate limiting."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "") or request.args.get("token")
    if not token:
        emit("error", {"message": "Token required"})
        return

    try:
        decoded = decode_token(token)
        user_id = decoded["sub"]
    except Exception as e:
        logger.error(f"Send message - invalid token: {str(e)}")
        emit("error", {"message": "Invalid authentication"})
        return

    booking_id = data.get("booking_id")
    content = data.get("content", "").strip()
    is_file = data.get("is_file", False)
    file_url = data.get("file_url")

    if not booking_id or (not content and not is_file):
        emit("error", {"message": "Missing booking_id or content"})
        return

    if is_rate_limited(user_id):
        emit("error", {"message": "Rate limit exceeded - please wait"})
        return

    try:
        message = {
            "booking_id": booking_id,
            "sender_id": user_id,
            "receiver_id": data.get("receiver_id"),
            "content": content,
            "is_file": is_file,
            "file_url": file_url,
            "created_at": datetime.utcnow().isoformat(),
            "mime_type": data.get("mime_type"),
            "duration": data.get("duration")
        }

        res = supabase.table("messages").insert(message).execute()
        saved_message = res.data[0] if res.data else message

        emit("new_message", saved_message, room=f"conv_{booking_id}")

        logger.info(f"Message sent in conv_{booking_id} by {user_id}")

    except Exception as e:
        logger.error(f"Failed to send message: {str(e)}", exc_info=True)
        emit("error", {"message": "Failed to send message"})


# ────────────────────────────────────────────────
# Real-time Notification Functions (called from routes)
# ────────────────────────────────────────────────

def notify_request_update(request_id: str, buyer_id: str, status: str, message: str, extra: dict = None):
    """
    Notify the specific buyer about a job request status change.
    Safe even if Redis or Socket.IO is down.
    """
    payload = {
        "type": "request_update",
        "request_id": request_id,
        "status": status,
        "message": message,
        **(extra or {})
    }

    try:
        socketio.emit("request_update", payload, room=f"user_{buyer_id}")
        logger.info(f"Emitted request_update to user_{buyer_id}: {status}")
    except Exception as e:
        logger.warning(f"Failed to emit request_update: {str(e)}")


def notify_new_offer(offer_id: str, request_id: str, buyer_id: str, seller_id: str, offered_price: float, message: str = None):
    """
    Notify buyer when a new offer is submitted for their request.
    """
    payload = {
        "type": "new_offer",
        "offer_id": offer_id,
        "request_id": request_id,
        "seller_id": seller_id,
        "offered_price": offered_price,
        "message": message or "A seller has submitted an offer for your request"
    }

    try:
        socketio.emit("new_offer", payload, room=f"user_{buyer_id}")
        logger.info(f"Emitted new_offer to user_{buyer_id} for request {request_id}")
    except Exception as e:
        logger.warning(f"Failed to emit new_offer: {str(e)}")


def notify_admin_new_request(request_id: str, buyer_id: str, title: str):
    """
    Optional: Broadcast to all admins when a new request is created.
    Useful if admins monitor incoming requests in real-time.
    """
    payload = {
        "type": "new_request",
        "request_id": request_id,
        "buyer_id": buyer_id,
        "title": title,
        "message": f"New job request from buyer: {title}"
    }

    try:
        socketio.emit("admin_new_request", payload, room="admins")
        logger.info(f"Emitted admin_new_request for {request_id}")
    except Exception as e:
        logger.warning(f"Failed to emit admin_new_request: {str(e)}")


def init_socketio(socketio_instance):
    """
    Optional initialization hook (called from __init__.py if needed).
    Ensures Redis is used for message queue if available.
    """
    if redis_client is None:
        logger.warning("Redis unavailable - Socket.IO pub/sub disabled")
        socketio_instance.message_queue = None
    else:
        logger.info("Socket.IO using Redis pub/sub")