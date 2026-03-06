# app/routes/buyer.py
from functools import wraps
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import get_jwt, jwt_required, get_jwt_identity
from app.services.supabase_service import supabase
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
import uuid, os , time, logging
from app.utils.audit import log_action
from app import socketio
from postgrest import exceptions as postgrest_exceptions
from app.extensions import limiter
from app.socket_handlers import notify_request_update

bp = Blueprint("buyer", __name__, url_prefix="/api/buyer")

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_AVATAR_SIZE = 5 * 1024 * 1024  # 5MB


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ────────────────────────────────────────────────
# GET /api/buyer/dashboard
# Trending categories + featured sellers
# ────────────────────────────────────────────────
@bp.route("/dashboard", methods=["GET"])
@jwt_required(optional=True)
def buyer_dashboard():
    try:
        # Trending categories (last 30 days)
        gigs = supabase.table("gigs")\
            .select("category")\
            .gte("created_at", (datetime.utcnow() - timedelta(days=30)).isoformat())\
            .execute().data or []

        from collections import Counter
        category_count = Counter(gig.get("category") for gig in gigs if gig.get("category"))
        
        trending_categories = [
            {"name": name, "count": count}
            for name, count in category_count.most_common(8)
        ]

        # Featured sellers (verified + highest rated, limit 6)
        sellers_res = supabase.table("profiles")\
            .select("id, full_name, rating, avatar_url, is_verified")\
            .eq("role", "seller")\
            .eq("is_verified", True)\
            .order("rating", desc=True)\
            .limit(6)\
            .execute()

        featured_sellers = []
        for seller in (sellers_res.data or []):
            min_price_res = supabase.table("gigs")\
                .select("price")\
                .eq("seller_id", seller["id"])\
                .order("price")\
                .limit(1)\
                .maybe_single()\
                .execute()

            min_price = (
                min_price_res.data["price"]
                if min_price_res.data and "price" in min_price_res.data
                else 250
            )

            featured_sellers.append({
                "id": seller["id"],
                "full_name": seller["full_name"] or "Unnamed Seller",
                "rating": seller["rating"] or 0.0,
                "avatar_url": seller["avatar_url"],
                "starting_price": min_price,
                "is_verified": seller["is_verified"]
            })

        return jsonify({
            "trendingCategories": trending_categories,
            "featuredVAs": featured_sellers
        }), 200

    except Exception as e:
        current_app.logger.exception("Buyer dashboard failed")
        return jsonify({
            "error": "Failed to load dashboard data",
            "details": str(e) if current_app.debug else None
        }), 500


# ────────────────────────────────────────────────
# GET /api/buyer/conversations
# List buyer conversations
# ────────────────────────────────────────────────
@bp.route("/conversations", methods=["GET"])
@jwt_required()
def buyer_conversations():
    buyer_id = get_jwt_identity()
    try:
        bookings = supabase.table("bookings")\
            .select("""
                id, status, start_time,
                seller:seller_id (id, full_name, avatar_url)
            """)\
            .eq("buyer_id", buyer_id)\
            .in_("status", ["pending", "in_progress", "completed"])\
            .order("start_time", desc=True)\
            .execute().data or []

        conversations = []
        for b in bookings:
            conversations.append({
                "id": b["id"],
                "seller": {
                    "id": b["seller"]["id"],
                    "name": b["seller"]["full_name"] or "Seller",
                    "avatar": b["seller"]["avatar_url"]
                },
                "last_message": "Booking update or message",
                "last_message_time": b["start_time"],
                "unread_count": 0,
                "status": b["status"]
            })

        return jsonify(conversations), 200

    except Exception as e:
        current_app.logger.error(f"Conversations error: {str(e)}")
        return jsonify({"error": "Failed to load conversations"}), 500


# ────────────────────────────────────────────────
# GET /api/buyer/bookings
# All bookings for current buyer
# ────────────────────────────────────────────────
@bp.route("/bookings", methods=["GET"])
@jwt_required()
def buyer_bookings():
    buyer_id = get_jwt_identity()
    try:
        res = supabase.table("bookings")\
            .select("""
                id, status, price, requirements, created_at, updated_at,
                gig:gig_id (id, title, price),
                seller:seller_id (id, full_name, avatar_url),
                reviews!booking_id (id)
            """)\
            .eq("buyer_id", buyer_id)\
            .order("created_at", desc=True)\
            .execute()

        bookings = []
        for row in (res.data or []):
            bookings.append({
                "id": row["id"],
                "status": row["status"],
                "price": row["price"],
                "requirements": row["requirements"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "gig": row.get("gig") or {"title": "Untitled Gig", "price": row["price"]},
                "seller": row.get("seller") or {"full_name": "Unknown", "avatar_url": None},
                "reviewed": bool(row.get("reviews"))
            })

        return jsonify(bookings), 200

    except Exception as e:
        current_app.logger.error(f"Bookings error: {str(e)}")
        return jsonify({"error": "Failed to load bookings"}), 500


# ────────────────────────────────────────────────
# PATCH /api/buyer/bookings/:id/cancel
# Cancel pending booking
# ────────────────────────────────────────────────
@bp.route("/bookings/<string:id>/cancel", methods=["PATCH"])
@jwt_required()
def cancel_booking(id):
    buyer_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "").strip()

    if not reason or len(reason) < 10:
        return jsonify({"error": "Cancellation reason must be at least 10 characters"}), 400

    try:
        booking = supabase.table("bookings")\
            .select("id, status, buyer_id")\
            .eq("id", id)\
            .maybe_single().execute().data

        if not booking:
            return jsonify({"error": "Booking not found"}), 404

        if booking["buyer_id"] != buyer_id:
            return jsonify({"error": "Unauthorized"}), 403

        if booking["status"] != "pending":
            return jsonify({"error": "Only pending bookings can be cancelled"}), 400

        supabase.table("bookings")\
            .update({
                "status": "cancelled",
                "cancel_reason": reason,
                "updated_at": "now()"
            })\
            .eq("id", id)\
            .execute()

        log_action(
            actor_id=buyer_id,
            action="cancel_booking",
            target_id=id,
            details={"reason": reason}
        )

        return jsonify({"message": "Booking cancelled successfully"}), 200

    except Exception as e:
        current_app.logger.error(f"Cancel booking error: {str(e)}")
        return jsonify({"error": "Failed to cancel booking"}), 500

# ────────────────────────────────────────────────
# GET /api/buyer/profile/:id/bookings
# Get recent bookings for a buyer
# ────────────────────────────────────────────────
@bp.route("/profile/<string:user_id>/bookings", methods=["GET"])
@jwt_required()
def get_buyer_bookings(user_id: str):
    current_user = get_jwt_identity()
    limit = request.args.get("limit", 5, type=int)

    if current_user != user_id:
        return jsonify({"error": "You can only view your own bookings"}), 403

    try:
        bookings = supabase.table("bookings")\
            .select("""
                id,
                gig_id,
                seller_id,
                status,
                price,
                created_at,
                gig:gig_id (title),
                seller:seller_id (full_name)
            """)\
            .eq("buyer_id", user_id)\
            .order("created_at", desc=True)\
            .limit(limit)\
            .execute()

        formatted = []
        for b in (bookings.data or []):
            formatted.append({
                "id": b["id"],
                "gig_title": b["gig"]["title"] if b["gig"] else "Untitled Gig",
                "seller_name": b["seller"]["full_name"] if b["seller"] else "Unknown Seller",
                "status": b["status"],
                "price": b["price"] or 0,
                "created_at": b["created_at"]
            })

        return jsonify(formatted), 200

    except Exception as e:
        current_app.logger.exception(f"Bookings fetch failed for buyer {user_id}")
        return jsonify({"error": "Failed to load bookings"}), 500


# ────────────────────────────────────────────────
# POST /api/buyer/reviews
# Submit a review for a completed booking
# ────────────────────────────────────────────────
@bp.route("/reviews", methods=["POST"])
@jwt_required()
def create_review():
    buyer_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    booking_id = data.get("booking_id")
    rating = data.get("rating")
    comment = (data.get("comment") or "").strip()

    if not booking_id:
        return jsonify({"error": "booking_id is required"}), 400

    if not isinstance(rating, (int, float)) or not (1 <= rating <= 5):
        return jsonify({"error": "Rating must be a number between 1 and 5"}), 400

    try:
        booking_res = supabase.table("bookings")\
            .select("id, status, buyer_id, seller_id")\
            .eq("id", booking_id)\
            .maybe_single().execute()

        if not booking_res.data:
            return jsonify({"error": "Booking not found"}), 404

        booking = booking_res.data

        if booking["buyer_id"] != buyer_id:
            return jsonify({"error": "Not your booking"}), 403

        if booking["status"] != "completed":
            return jsonify({"error": "Can only review completed bookings"}), 400

        existing = supabase.table("reviews")\
            .select("id")\
            .eq("booking_id", booking_id)\
            .maybe_single().execute()

        if existing.data:
            return jsonify({"error": "You have already reviewed this booking"}), 409

        review_data = {
            "id": str(uuid.uuid4()),
            "booking_id": booking_id,
            "reviewer_id": buyer_id,
            "reviewed_id": booking["seller_id"],
            "rating": float(rating),
            "comment": comment if comment else None,
            "created_at": "now()"
        }

        insert_res = supabase.table("reviews").insert(review_data).execute()

        if not insert_res.data:
            return jsonify({"error": "Failed to create review"}), 500

        # Recalculate seller rating
        recalculate_seller_rating(booking["seller_id"])

        log_action(
            actor_id=buyer_id,
            action="submit_review",
            target_id=booking_id,
            details={"rating": rating}
        )

        return jsonify({
            "message": "Review submitted successfully",
            "review_id": insert_res.data[0]["id"]
        }), 201

    except Exception as e:
        current_app.logger.exception("Review creation failed")
        return jsonify({"error": "Failed to submit review"}), 500


# ────────────────────────────────────────────────
# Helper: recalculate seller rating
# ────────────────────────────────────────────────
def recalculate_seller_rating(seller_id: str):
    try:
        ratings_res = supabase.table("reviews")\
            .select("rating")\
            .eq("reviewed_id", seller_id)\
            .execute()

        ratings = [r["rating"] for r in (ratings_res.data or []) if isinstance(r["rating"], (int, float))]
        count = len(ratings)

        avg = round(sum(ratings) / count, 1) if count > 0 else 0.0

        supabase.table("profiles")\
            .update({
                "average_rating": avg,
                "review_count": count,
                "updated_at": "now()"
            })\
            .eq("id", seller_id)\
            .execute()

        logger.info(f"Recalculated rating for seller {seller_id}: {avg} ({count} reviews)")

    except Exception as e:
        logger.error(f"Rating recalculation failed for {seller_id}: {str(e)}")


# ────────────────────────────────────────────────
# GET /api/sellers/search?q=...
# Search sellers
# ────────────────────────────────────────────────
@bp.route("/sellers/search", methods=["GET"])
@jwt_required(optional=True)
def sellers_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])

    try:
        res = supabase.table("profiles")\
            .select("id, full_name, avatar_url, rating, is_verified")\
            .eq("role", "seller")\
            .ilike("full_name", f"%{q}%")\
            .limit(10)\
            .execute()

        return jsonify(res.data or []), 200

    except Exception as e:
        current_app.logger.error(f"Seller search error: {str(e)}")
        return jsonify({"error": "Failed to search sellers"}), 500


# ────────────────────────────────────────────────
# POST /api/buyer/messages/start
# Start conversation with seller
# ────────────────────────────────────────────────
@bp.route("/messages/start", methods=["POST"])
@jwt_required()
def start_message():
    buyer_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    receiver_id = data.get("receiver_id")
    content = (data.get("content") or "").strip()

    if not receiver_id or not content:
        return jsonify({"error": "receiver_id and content required"}), 400

    try:
        receiver = supabase.table("profiles")\
            .select("role")\
            .eq("id", receiver_id)\
            .maybe_single().execute()

        if not receiver.data or receiver.data["role"] != "seller":
            return jsonify({"error": "Can only message sellers"}), 400

        message = {
            "sender_id": buyer_id,
            "receiver_id": receiver_id,
            "content": content,
            "created_at": "now()"
        }

        insert_res = supabase.table("messages").insert(message).execute()

        if not insert_res.data:
            return jsonify({"error": "Failed to send initial message"}), 500

        socketio.emit("new_message", {
            "sender_id": buyer_id,
            "receiver_id": receiver_id,
            "content": content,
            "created_at": datetime.utcnow().isoformat()
        }, room=f"user_{receiver_id}")

        log_action(
            actor_id=buyer_id,
            action="start_conversation",
            details={"with": receiver_id}
        )

        return jsonify({
            "message": "Conversation started",
            "conversation_id": receiver_id
        }), 201

    except Exception as e:
        logger.exception("Start message failed")
        return jsonify({"error": "Failed to start conversation"}), 500


# ────────────────────────────────────────────────
# GET /api/buyer/messages/conversation/<conversation_id>
# Get message history
# ────────────────────────────────────────────────
@bp.route("/messages/conversation/<string:conversation_id>", methods=["GET"])
@jwt_required()
def get_buyer_chat_history(conversation_id):
    user_id = get_jwt_identity()
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)

    try:
        participant = supabase.table("messages")\
            .select("id")\
            .or_(f"sender_id.eq.{user_id},receiver_id.eq.{user_id}")\
            .eq("receiver_id", conversation_id)\
            .limit(1).execute()

        if not participant.data:
            return jsonify({"error": "Conversation not found or unauthorized"}), 403

        res = supabase.table("messages")\
            .select("""
                id, content, created_at, sender_id, receiver_id, is_file, file_url, mime_type, file_name, read_at,
                sender:profiles!sender_id (full_name, avatar_url),
                receiver:profiles!receiver_id (full_name, avatar_url)
            """)\
            .or_(f"sender_id.eq.{user_id},receiver_id.eq.{user_id}")\
            .eq("receiver_id", conversation_id)\
            .order("created_at", asc=True)\
            .range(offset, offset + limit - 1).execute()

        formatted = []
        for msg in (res.data or []):
            formatted.append({
                "id": msg["id"],
                "content": msg["content"],
                "created_at": msg["created_at"],
                "sender_id": msg["sender_id"],
                "receiver_id": msg["receiver_id"],
                "is_file": msg["is_file"],
                "file_url": msg["file_url"],
                "mime_type": msg["mime_type"],
                "file_name": msg["file_name"],
                "read_at": msg["read_at"],
                "sender": {
                    "id": msg["sender_id"],
                    "name": msg["sender"]["full_name"] if msg["sender"] else "Unknown",
                    "avatar": msg["sender"]["avatar_url"]
                },
                "receiver": {
                    "id": msg["receiver_id"],
                    "name": msg["receiver"]["full_name"] if msg["receiver"] else "Unknown",
                    "avatar": msg["receiver"]["avatar_url"]
                },
                "is_sent_by_me": msg["sender_id"] == user_id
            })

        return jsonify(formatted), 200

    except Exception as e:
        logger.exception(f"Buyer chat history error for {conversation_id}")
        return jsonify({"error": "Failed to load messages"}), 500


# ────────────────────────────────────────────────
# POST /api/messages/upload
# Upload file for chat
# ────────────────────────────────────────────────
@bp.route("/messages/upload", methods=["POST"])
@jwt_required()
def upload_message_file():
    user_id = get_jwt_identity()

    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4()}_{filename}"
        file_path = os.path.join("uploads/messages", unique_filename)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        file.save(file_path)

        file_url = f"/uploads/messages/{unique_filename}"  # adjust to your static serving

        mime_type = file.mimetype
        return jsonify({
            "url": file_url,
            "mime_type": mime_type,
            "file_name": filename
        }), 200

    return jsonify({"error": "File type not allowed"}), 400


# ────────────────────────────────────────────────
# Notifications
# ────────────────────────────────────────────────
@bp.route("/notifications", methods=["GET"])
@jwt_required()
def get_notifications():
    user_id = get_jwt_identity()
    limit = request.args.get("limit", 20, type=int)
    try:
        res = supabase.table("notifications")\
            .select("""
                id, type, content, created_at, read_at,
                sender:profiles!sender_id (full_name, avatar_url),
                related_id
            """)\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(limit).execute()

        notifications = []
        for n in (res.data or []):
            notifications.append({
                "id": n["id"],
                "type": n["type"],
                "content": n["content"],
                "created_at": n["created_at"],
                "read": bool(n["read_at"]),
                "sender": n["sender"],
                "related_id": n["related_id"]
            })

        return jsonify(notifications), 200

    except Exception as e:
        current_app.logger.error(f"Notifications error: {str(e)}")
        return jsonify({"error": "Failed to load notifications"}), 500


@bp.route("/notifications/unread-count", methods=["GET"])
@jwt_required()
def get_unread_count():
    user_id = get_jwt_identity()
    try:
        count_res = supabase.table("notifications")\
            .select("count", count="exact")\
            .eq("user_id", user_id)\
            .is_("read_at", None).execute()

        return jsonify({"unread_count": count_res.count or 0}), 200

    except Exception as e:
        current_app.logger.error(f"Unread count error: {str(e)}")
        return jsonify({"error": "Failed to get unread count"}), 500


@bp.route("/notifications/mark-read", methods=["PATCH"])
@jwt_required()
def mark_notifications_read():
    user_id = get_jwt_identity()
    data = request.get_json()
    notification_id = data.get("id")
    try:
        query = supabase.table("notifications")\
            .update({"read_at": "now()"})\
            .eq("user_id", user_id)\
            .is_("read_at", None)

        if notification_id:
            query = query.eq("id", notification_id)

        res = query.execute()
        return jsonify({"message": f"{res.count or 0} notification(s) marked as read"}), 200

    except Exception as e:
        current_app.logger.error(f"Mark read error: {str(e)}")
        return jsonify({"error": "Failed to mark notifications as read"}), 500


# ────────────────────────────────────────────────
# GET /api/profile/:id
# Public profile info
# ────────────────────────────────────────────────
@bp.route("/profile/<string:user_id>", methods=["GET"])
def get_profile(user_id: str):
    try:
        profile = supabase.table("profiles")\
            .select("""
                id,
                full_name,
                role,
                bio,
                avatar_url,
                phone,
                created_at,
                updated_at,
                is_verified
            """)\
            .eq("id", user_id)\
            .maybe_single()\
            .execute()

        if not profile.data:
            return jsonify({"error": "Profile not found"}), 404

        reviews = supabase.table("reviews")\
            .select("rating")\
            .eq("reviewed_id", user_id)\
            .execute().data or []

        review_count = len(reviews)
        avg_rating = round(sum(r["rating"] for r in reviews) / review_count, 1) if review_count > 0 else 0.0

        return jsonify({
            **profile.data,
            "average_rating": avg_rating,
            "review_count": review_count,
            "interests": []  # placeholder
        }), 200

    except Exception as e:
        current_app.logger.exception(f"Profile fetch failed for user {user_id}")
        return jsonify({"error": "Failed to load profile"}), 500


# ────────────────────────────────────────────────
# PATCH /api/profile/:id
# Update own profile
# ────────────────────────────────────────────────
@bp.route("/profile/<string:user_id>", methods=["PATCH"])
@jwt_required()
def update_profile(user_id: str):
    current_user = get_jwt_identity()

    if current_user != user_id:
        return jsonify({"error": "You can only update your own profile"}), 403

    data = request.get_json(silent=True) or {}

    allowed = ["full_name", "bio", "phone", "interests"]
    update_data = {k: v for k, v in data.items() if k in allowed and v is not None}

    if not update_data:
        return jsonify({"error": "No valid fields to update"}), 400

    if "full_name" in update_data:
        name = (update_data["full_name"] or "").strip()
        if len(name) < 2 or len(name) > 100:
            return jsonify({"error": "Full name must be 2–100 characters"}), 400
        update_data["full_name"] = name

    if "phone" in update_data:
        phone = (update_data["phone"] or "").strip()
        update_data["phone"] = phone if phone else None

    if "bio" in update_data:
        bio = (update_data["bio"] or "").strip()
        if len(bio) > 1000:
            return jsonify({"error": "Bio cannot exceed 1000 characters"}), 400
        update_data["bio"] = bio

    if "interests" in update_data:
        if not isinstance(update_data["interests"], list):
            return jsonify({"error": "Interests must be a list"}), 400
        update_data["interests"] = [i.strip() for i in update_data["interests"] if i.strip()]

    try:
        res = supabase.table("profiles")\
            .update({**update_data, "updated_at": "now()"})\
            .eq("id", user_id)\
            .execute()

        if not res.data:
            return jsonify({"error": "Profile not found"}), 404

        log_action(
            actor_id=current_user,
            action="update_profile",
            details={"updated_fields": list(update_data.keys())}
        )

        return jsonify({
            "message": "Profile updated successfully",
            "updated_fields": list(update_data.keys())
        }), 200

    except Exception as e:
        current_app.logger.exception(f"Profile update failed for user {user_id}")
        return jsonify({"error": "Failed to update profile"}), 500


# ────────────────────────────────────────────────
# POST /api/profile/avatar
# Upload avatar
# ────────────────────────────────────────────────
@bp.route("/profile/avatar", methods=["POST"])
@jwt_required()
@limiter.limit("1 per 5 minutes")
def upload_avatar():
    user_id = get_jwt_identity()

    if "avatar" not in request.files:
        return jsonify({"error": "No avatar file provided"}), 400

    file = request.files["avatar"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": f"Allowed types: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    file_content = file.read()
    if len(file_content) > MAX_AVATAR_SIZE:
        return jsonify({"error": "File too large (max 5MB)"}), 400

    try:
        file.seek(0)
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = secure_filename(f"{user_id}_{uuid.uuid4().hex}.{ext}")
        path = f"avatars/{user_id}/{filename}"

        upload_res = supabase.storage.from_("avatars").upload(
            path=path,
            file=file_content,
            file_options={"cacheControl": "3600", "upsert": "true"}
        )

        if upload_res.status_code not in (200, 201):
            return jsonify({"error": "Failed to upload to storage"}), 500

        public_url = supabase.storage.from_("avatars").get_public_url(path)

        supabase.table("profiles").update({"avatar_url": public_url}).eq("id", user_id).execute()

        log_action(
            actor_id=user_id,
            action="upload_avatar"
        )

        return jsonify({"message": "Avatar uploaded", "publicUrl": public_url}), 200

    except Exception as e:
        current_app.logger.error(f"Avatar upload error: {str(e)}")
        return jsonify({"error": "Failed to upload avatar"}), 500


@bp.route("/debug/supabase", methods=["GET"])
def debug_supabase():
    status = supabase.check_connection()
    return jsonify(status), 200


# ────────────────────────────────────────────────
# GET /api/buyer/categories/<slug>
# Sellers in a category
# ────────────────────────────────────────────────
@bp.route("/categories/<slug>", methods=["GET"])
@jwt_required()
def get_category_sellers(slug):
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 9, type=int)
    min_rating = request.args.get("min_rating", 0, type=float)
    search = request.args.get("search", "")

    offset = (page - 1) * limit

    try:
        query = supabase.table("profiles")\
            .select("""
                id,
                full_name,
                avatar_url,
                rating,
                is_verified,
                is_online,
                gigs!seller_id (id, title, description, price, category)
            """)\
            .eq("role", "seller")\
            .filter("gigs.category", "eq", slug)

        if min_rating > 0:
            query = query.gte("rating", min_rating)

        if search:
            query = query.or_(
                f"full_name.ilike.%{search}%,"
                f"gigs.title.ilike.%{search}%"
            )

        query = query.order("rating", desc=True)

        res = query.range(offset, offset + limit - 1).execute()

        sellers_raw = res.data or []
        total = res.count or 0

        formatted_sellers = []
        for profile in sellers_raw:
            gigs = profile.pop("gigs", [])
            formatted_sellers.append({
                "seller": profile,
                "gigs": gigs,
                "reviewCount": profile.get("review_count", 0)
            })

        return jsonify({
            "sellers": formatted_sellers,
            "total": total,
            "page": page,
            "has_more": len(sellers_raw) == limit,
            "message": "No sellers found for this category" if not sellers_raw else None
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────
# Saved Sellers
# ────────────────────────────────────────────────

@bp.route("/saved/<seller_id>", methods=["GET"])
@jwt_required()
def check_saved_seller(seller_id):
    user_id = get_jwt_identity()
    try:
        res = supabase.table("saved_sellers")\
            .select("id")\
            .eq("buyer_id", user_id)\
            .eq("seller_id", seller_id)\
            .execute()
        return jsonify({"saved": bool(res.data)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/saved", methods=["POST"])
@jwt_required()
def save_seller():
    user_id = get_jwt_identity()
    data = request.get_json()
    if not data or "seller_id" not in data:
        return jsonify({"error": "seller_id is required"}), 400

    seller_id = data["seller_id"]
    try:
        check = supabase.table("saved_sellers")\
            .select("id")\
            .eq("buyer_id", user_id)\
            .eq("seller_id", seller_id)\
            .execute()
        if check.data:
            return jsonify({"message": "Already saved"}), 200

        entry = {
            "id": str(uuid.uuid4()),
            "buyer_id": user_id,
            "seller_id": seller_id,
            "created_at": "now()"
        }
        res = supabase.table("saved_sellers").insert(entry).execute()

        log_action(
            actor_id=user_id,
            action="save_seller",
            target_id=seller_id
        )

        return jsonify({"message": "Seller saved"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/saved/<seller_id>", methods=["DELETE"])
@jwt_required()
def unsave_seller(seller_id):
    user_id = get_jwt_identity()
    try:
        res = supabase.table("saved_sellers")\
            .delete()\
            .eq("buyer_id", user_id)\
            .eq("seller_id", seller_id)\
            .execute()

        log_action(
            actor_id=user_id,
            action="unsave_seller",
            target_id=seller_id
        )

        return jsonify({"message": "Seller unsaved"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────
# Buyer Requests
# ────────────────────────────────────────────────
@bp.route("/requests", methods=["GET"])
@jwt_required()
def get_buyer_requests():
    user_id = get_jwt_identity()

    try:
        res = supabase.table("job_requests")\
            .select("*")\
            .eq("buyer_id", user_id)\
            .order("created_at", desc=True)\
            .execute()

        return jsonify(res.data), 200
    except Exception as e:
        logger.error(f"Failed to fetch buyer requests: {str(e)}")
        return jsonify({"error": "Failed to load requests"}), 500


@bp.route("/requests", methods=["POST"])
@jwt_required()
def create_buyer_request():
    """
    Create a new job request from buyer to be reviewed by admin.
    Required: category, title, description
    Optional: budget, preferred_start_time, estimated_due_time, seller_id
    """
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    # Validate required fields
    required = ["category", "title", "description"]
    missing = [f for f in required if f not in data or not data[f] or not str(data[f]).strip()]
    if missing:
        logger.warning(f"Missing required fields from user {user_id}: {missing}")
        return jsonify({
            "error": "Missing required fields",
            "missing": missing,
            "required": required
        }), 400

    try:
        # Sanitize and prepare request data
        req = {
            "id": str(uuid.uuid4()),
            "buyer_id": user_id,
            "category": str(data["category"]).strip(),
            "title": str(data["title"]).strip(),
            "description": str(data["description"]).strip(),
            # Optional fields with safe conversion
            "budget": float(data["budget"]) if data.get("budget") and str(data["budget"]).strip() else None,
            "preferred_start_time": data.get("preferred_start_time") or None,
            "estimated_due_time": data.get("estimated_due_time") or None,
            "seller_id": data.get("seller_id") or None,
            "status": "pending_admin",  # Changed to match your CHECK constraint
            "created_at": "now()"
        }

        # Debug log
        logger.info(f"Creating job request | buyer={user_id} | seller={req.get('seller_id')} | title={req['title'][:50]}... | status={req['status']}")

        # Perform insert
        res = supabase.table("job_requests").insert(req).execute()

        if not res.data or len(res.data) == 0:
            logger.error(f"Insert returned no data for buyer {user_id} - possible RLS, constraint, or DB error")
            return jsonify({
                "error": "Failed to create request",
                "details": "No data returned from database (check RLS or constraints)"
            }), 500

        request_id = res.data[0]["id"]

        # Log success
        log_action(
            actor_id=user_id,
            action="create_job_request",
            details={
                "request_id": request_id,
                "title": req["title"],
                "category": req["category"],
                "seller_id": req.get("seller_id"),
                "budget": req.get("budget")
            }
        )

        # ── REAL-TIME NOTIFICATION: New request created ──────────────────────
        notify_request_update(
            request_id=request_id,
            buyer_id=user_id,
            status="pending_admin",
            message="Your job request has been submitted and is pending admin review",
            extra={"title": req["title"]}
        )

        logger.info(f"Job request created successfully | id={request_id} | buyer={user_id}")

        return jsonify({
            "message": "Request submitted successfully",
            "request_id": request_id,
            "status": req["status"]
        }), 201

    except postgrest_exceptions.APIError as e:
        logger.error(f"Supabase API error during insert: {e.code} - {e.message} | user={user_id}")
        error_details = {
            "code": e.code,
            "message": e.message,
            "hint": getattr(e, 'hint', None)
        }

        if e.code == "42501":  # permission denied (RLS)
            return jsonify({
                "error": "Permission denied",
                "details": "RLS policy violation - check job_requests table policies"
            }), 403

        if e.code == "23514":  # check violation (e.g. invalid status)
            return jsonify({
                "error": "Invalid request data",
                "details": f"Value violates database constraints: {e.message}"
            }), 400

        if e.code in ("23505", "23503"):  # unique / foreign key violation
            return jsonify({
                "error": "Invalid request data",
                "details": str(e)
            }), 400

        return jsonify({
            "error": "Database error",
            "details": error_details
        }), 500

    except ValueError as ve:
        # Budget or other conversion failed
        logger.error(f"Value error in request data: {str(ve)} | raw data: {data}")
        return jsonify({
            "error": "Invalid field value",
            "details": str(ve)
        }), 400

    except Exception as e:
        logger.exception(f"Unexpected error creating job request for user {user_id}")
        return jsonify({
            "error": "Internal server error",
            "message": "Something went wrong - please try again later"
        }), 500


@bp.route("/requests/<request_id>/cancel", methods=["PATCH"])
@jwt_required()
def cancel_buyer_request(request_id):
    """
    Allow buyer to cancel their own pending request.
    """
    user_id = get_jwt_identity()

    try:
        # Fetch request to verify ownership and status
        req_res = supabase.table("job_requests")\
            .select("buyer_id, status")\
            .eq("id", request_id)\
            .maybe_single()\
            .execute()

        if not req_res.data:
            return jsonify({"error": "Request not found"}), 404

        request_data = req_res.data

        if request_data["buyer_id"] != user_id:
            return jsonify({"error": "You do not own this request"}), 403

        if request_data["status"] not in ("pending_admin", "pending"):
            return jsonify({"error": "Cannot cancel request in current status"}), 400

        # Update status
        update_res = supabase.table("job_requests")\
            .update({
                "status": "cancelled",
                "updated_at": "now()"
            })\
            .eq("id", request_id)\
            .execute()

        if not update_res.data:
            return jsonify({"error": "Failed to cancel request"}), 500

        # ── REAL-TIME NOTIFICATION: Request cancelled ───────────────────────
        notify_request_update(
            request_id=request_id,
            buyer_id=user_id,
            status="cancelled",
            message="Your job request has been cancelled"
        )

        log_action(
            actor_id=user_id,
            action="cancel_job_request",
            details={"request_id": request_id}
        )

        return jsonify({"message": "Request cancelled successfully"}), 200

    except Exception as e:
        logger.exception(f"Error cancelling request {request_id} by user {user_id}")
        return jsonify({"error": "Failed to cancel request"}), 500

# ────────────────────────────────────────────────
# ADDED: GET /api/buyer/gigs/<id>
# Buyer-specific gig detail fetch (can be extended later)
# ────────────────────────────────────────────────
@bp.route("/gigs/<string:id>", methods=["GET"])
@jwt_required()
def get_buyer_gig(id: str):
    """
    Buyer-specific endpoint: Fetch details for a single gig
    (Currently same as public, but can add buyer-specific data later, e.g. booking status)
    """
    buyer_id = get_jwt_identity()

    try:
        res = supabase.table("gigs")\
            .select("""
                id,
                title,
                description,
                price,
                category,
                gallery_urls,
                created_at,
                seller:seller_id (full_name, avatar_url, is_verified, rating)
            """)\
            .eq("id", id)\
            .maybe_single()\
            .execute()

        if not res.data:
            return jsonify({"error": "Gig not found"}), 404

        gig = res.data
        seller = gig.pop("seller", {}) or {}

        # Optional: Check if buyer has already booked this gig (future feature)
        # has_booked = supabase.table("bookings")\
        #     .select("id")\
        #     .eq("buyer_id", buyer_id)\
        #     .eq("gig_id", id)\
        #     .limit(1)\
        #     .execute().data is not None

        return jsonify({
            **gig,
            "seller_name": seller.get("full_name", "Unknown"),
            "seller_avatar": seller.get("avatar_url"),
            "seller_is_verified": seller.get("is_verified", False),
            "seller_rating": seller.get("rating", 0.0),
            # "has_booked": has_booked,  # uncomment when you add this logic
        }), 200

    except Exception as e:
        current_app.logger.exception(f"Buyer gig fetch failed for ID {id}")
        return jsonify({"error": "Failed to load gig details"}), 500
    
@bp.route("/debug/token", methods=["GET"])
@jwt_required()
def debug_token():
    return jsonify({
        "user_id": get_jwt_identity(),
        "claims": get_jwt()
    }), 200