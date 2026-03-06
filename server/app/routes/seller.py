# app/routes/seller.py
from functools import wraps
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
import postgrest
import socketio
from app.services.supabase_service import supabase
from datetime import datetime
from werkzeug.utils import secure_filename
import uuid
import logging
from app.utils.audit import log_action
import time
from app.extensions import limiter

bp = Blueprint("seller", __name__, url_prefix="/api/seller")

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def seller_required(f):
    @wraps(f)
    @jwt_required()
    def decorated(*args, **kwargs):
        user_id = get_jwt_identity()
        profile = supabase.table("profiles")\
            .select("role")\
            .eq("id", user_id)\
            .maybe_single()\
            .execute()

        if not profile.data or profile.data.get("role") != "seller":
            return jsonify({"error": "Seller access required"}), 403

        return f(*args, **kwargs)
    return decorated

def safe_query(query, retries=3, delay=1):
    for attempt in range(retries):
        try:
            return query.execute()
        except Exception as e:
            if "disconnected" in str(e) or "timeout" in str(e).lower():
                logger.warning(f"Supabase disconnected (attempt {attempt+1}/{retries})")
                time.sleep(delay * (attempt + 1))  # exponential backoff
                continue
            raise
    raise Exception("Supabase query failed after retries")

def safe_supabase_query(query, retries=3, backoff=1):
    for attempt in range(retries):
        try:
            return query.execute()
        except Exception as e:
            if "disconnected" in str(e) or "timeout" in str(e).lower():
                logger.warning(f"Supabase disconnected (attempt {attempt+1}/{retries})")
                time.sleep(backoff * (attempt + 1))
                continue
            raise
    raise Exception("Supabase query failed after retries")


# ────────────────────────────────────────────────
# POST /api/seller/gigs
# Create new gig (seller only)
# ────────────────────────────────────────────────
@bp.route("/gigs", methods=["POST"])
@jwt_required()
@seller_required
def create_gig():
    seller_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    required = ["title", "category", "description", "price"]
    for field in required:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    title = data["title"].strip()
    description = data["description"].strip()
    price = data["price"]
    category = data["category"]
    gallery_urls = data.get("gallery_urls", []) or []

    # Input validation
    if len(title) < 8 or len(title) > 80:
        return jsonify({"error": "Title must be 8–80 characters"}), 400

    if len(description) < 120 or len(description) > 5000:
        return jsonify({"error": "Description must be 120–5000 characters"}), 400

    try:
        price = float(price)
        if price < 50 or price > 2000:
            return jsonify({"error": "Price must be between R50 and R2000"}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "Price must be a valid number"}), 400

    # Validate gallery_urls
    if not isinstance(gallery_urls, list):
        return jsonify({"error": "gallery_urls must be an array"}), 400
    if len(gallery_urls) > 5:
        return jsonify({"error": "Maximum 5 images allowed"}), 400
    if any(not isinstance(url, str) or not url.startswith("http") for url in gallery_urls):
        return jsonify({"error": "Invalid gallery URL format"}), 400

    try:
        gig = {
            "seller_id": seller_id,
            "title": title,
            "description": description,
            "price": price,
            "category": category,
            "gallery_urls": gallery_urls,
            "status": "published",
            "created_at": "now()",
            "updated_at": "now()"
        }

        res = supabase.table("gigs").insert(gig).execute()

        if not res.data:
            return jsonify({"error": "Failed to create gig"}), 500

        created_gig = res.data[0]
        gig_id = created_gig["id"]

        log_action(
            actor_id=seller_id,
            action="create_gig",
            target_id=gig_id,
            details={"title": title, "category": category, "price": price}
        )

        return jsonify({
            "message": "Gig created and published",
            "gig": created_gig  # ← return full gig object
        }), 201

    except Exception as e:
        current_app.logger.error(f"Gig creation error (seller {seller_id}): {str(e)}")
        return jsonify({"error": "Failed to create gig"}), 500

# ────────────────────────────────────────────────
# GET /api/seller/gigs
# List current seller's gigs (paginated)
# ────────────────────────────────────────────────
@bp.route("/gigs", methods=["GET"])
@jwt_required()
@seller_required
def list_seller_gigs():
    seller_id = get_jwt_identity()
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 12, type=int)

    from_idx = (page - 1) * per_page
    to_idx = from_idx + per_page - 1

    try:
        res = supabase.table("gigs")\
            .select("id, title, description, price, category, status, gallery_urls, created_at", count="exact")\
            .eq("seller_id", seller_id)\
            .range(from_idx, to_idx)\
            .order("created_at", desc=True)\
            .execute()

        return jsonify({
            "gigs": res.data or [],
            "total": res.count or 0,
            "page": page,
            "per_page": per_page,
            "has_more": len(res.data or []) == per_page
        }), 200

    except Exception as e:
        current_app.logger.error(f"List gigs error (seller {seller_id}): {str(e)}")
        return jsonify({"error": "Failed to load gigs"}), 500


# ────────────────────────────────────────────────
# GET /api/seller/gigs/:id
# Get single gig (owner only)
# ────────────────────────────────────────────────
@bp.route("/gigs/<string:id>", methods=["GET"])
@jwt_required()
@seller_required
def get_seller_gig(id):
    seller_id = get_jwt_identity()

    try:
        res = supabase.table("gigs")\
            .select("*")\
            .eq("id", id)\
            .eq("seller_id", seller_id)\
            .maybe_single()\
            .execute()

        if not res.data:
            return jsonify({"error": "Gig not found or not owned by you"}), 404

        return jsonify(res.data), 200

    except Exception as e:
        current_app.logger.error(f"Get gig error (seller {seller_id}, gig {id}): {str(e)}")
        return jsonify({"error": "Failed to load gig"}), 500


# ────────────────────────────────────────────────
# PATCH /api/seller/gigs/:id
# Update gig (owner only, limited fields)
# ────────────────────────────────────────────────
@bp.route("/gigs/<string:id>", methods=["PATCH"])
@jwt_required()
@seller_required
def update_seller_gig(id):
    seller_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    allowed_fields = ["title", "category", "description", "price", "gallery_urls", "status"]
    update_data = {k: v for k, v in data.items() if k in allowed_fields and v is not None}

    if not update_data:
        return jsonify({"error": "No valid fields to update"}), 400

    # Basic validation
    if "title" in update_data:
        title = str(update_data["title"]).strip()
        if len(title) < 8 or len(title) > 80:
            return jsonify({"error": "Title must be 8–80 characters"}), 400
        update_data["title"] = title

    if "description" in update_data:
        desc = str(update_data["description"]).strip()
        if len(desc) < 120 or len(desc) > 5000:
            return jsonify({"error": "Description must be 120–5000 characters"}), 400
        update_data["description"] = desc

    if "price" in update_data:
        try:
            price = float(update_data["price"])
            if price < 50 or price > 2000:
                return jsonify({"error": "Price must be between R50 and R2000"}), 400
            update_data["price"] = price
        except (ValueError, TypeError):
            return jsonify({"error": "Price must be a valid number"}), 400

    try:
        # Get old gig for audit
        old_gig = supabase.table("gigs")\
            .select("title, category, price")\
            .eq("id", id)\
            .eq("seller_id", seller_id)\
            .maybe_single().execute().data

        res = supabase.table("gigs")\
            .update({**update_data, "updated_at": "now()"})\
            .eq("id", id)\
            .eq("seller_id", seller_id)\
            .execute()

        if not res.data:
            return jsonify({"error": "Gig not found or not yours"}), 404

        # Audit log
        changes = {}
        if old_gig:
            for k in ["title", "category", "price"]:
                if k in update_data and old_gig.get(k) != update_data[k]:
                    changes[k] = {"old": old_gig.get(k), "new": update_data[k]}

        if changes:
            log_action(
                actor_id=seller_id,
                action="update_gig",
                target_id=id,
                details={"changes": changes}
            )

        return jsonify({"message": "Gig updated successfully"}), 200

    except Exception as e:
        current_app.logger.error(f"Update gig error (seller {seller_id}, gig {id}): {str(e)}")
        return jsonify({"error": "Failed to update gig"}), 500


# ────────────────────────────────────────────────
# DELETE /api/seller/gigs/:id
# Delete gig (owner only)
# ────────────────────────────────────────────────
@bp.route("/gigs/<string:id>", methods=["DELETE"])
@jwt_required()
@seller_required
def delete_seller_gig(id):
    seller_id = get_jwt_identity()

    try:
        gig = supabase.table("gigs")\
            .select("id, seller_id")\
            .eq("id", id)\
            .maybe_single().execute().data

        if not gig:
            return jsonify({"error": "Gig not found"}), 404

        if gig["seller_id"] != seller_id:
            return jsonify({"error": "Unauthorized"}), 403

        supabase.table("gigs").delete().eq("id", id).execute()

        log_action(
            actor_id=seller_id,
            action="delete_gig",
            target_id=id
        )

        return jsonify({"message": "Gig deleted successfully"}), 200

    except Exception as e:
        current_app.logger.error(f"Delete gig error (seller {seller_id}, gig {id}): {str(e)}")
        return jsonify({"error": "Failed to delete gig"}), 500


# ────────────────────────────────────────────────
# POST /api/seller/gig-images
# Upload gig gallery images (rate-limited)
# ────────────────────────────────────────────────
@bp.route("/gig-images", methods=["POST"])
@jwt_required()
@seller_required
@limiter.limit("1 per 10 minutes")  # Redis-backed
def upload_gig_images():
    seller_id = get_jwt_identity()

    if "images" not in request.files:
        return jsonify({"error": "No images provided"}), 400

    files = request.files.getlist("images")
    if not files or len(files) == 0:
        return jsonify({"error": "No images selected"}), 400

    uploaded_urls = []

    for file in files:
        if file.filename == "" or not allowed_file(file.filename):
            continue

        try:
            file_content = file.read()
            if len(file_content) > MAX_IMAGE_SIZE:
                return jsonify({"error": f"Image {file.filename} too large (max 5MB)"}), 400

            file.seek(0)

            ext = file.filename.rsplit('.', 1)[1].lower()
            filename = secure_filename(f"{seller_id}_{uuid.uuid4().hex}.{ext}")
            path = f"gig-gallery/{seller_id}/{filename}"

            upload_res = supabase.storage.from_("gig-gallery").upload(
                path=path,
                file=file_content,
                file_options={"cacheControl": "3600", "upsert": "true"}
            )

            if upload_res.status_code not in (200, 201):
                logger.warning(f"Storage upload failed for {path}: {upload_res}")
                continue

            public_url = supabase.storage.from_("gig-gallery").get_public_url(path)
            uploaded_urls.append(public_url)

        except Exception as e:
            logger.error(f"Gig image upload error (seller {seller_id}): {str(e)}")
            continue

    if uploaded_urls:
        log_action(
            actor_id=seller_id,
            action="upload_gig_images",
            details={"count": len(uploaded_urls), "urls": uploaded_urls}
        )

    return jsonify({
        "message": f"{len(uploaded_urls)} image(s) uploaded successfully",
        "urls": uploaded_urls
    }), 200


# ────────────────────────────────────────────────
# POST /api/seller/portfolio-images
# Upload portfolio images (rate-limited)
# ────────────────────────────────────────────────
@bp.route("/portfolio-images", methods=["POST"])
@jwt_required()
@seller_required
@limiter.limit("1 per 10 minutes")
def upload_portfolio_images():
    seller_id = get_jwt_identity()

    if "images" not in request.files:
        return jsonify({"error": "No images provided"}), 400

    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "No images selected"}), 400

    uploaded_urls = []

    for file in files:
        if file.filename == "" or not allowed_file(file.filename):
            continue

        try:
            file_content = file.read()
            if len(file_content) > MAX_IMAGE_SIZE:
                return jsonify({"error": f"Image {file.filename} too large (max 5MB)"}), 400

            file.seek(0)

            ext = file.filename.rsplit('.', 1)[1].lower()
            filename = secure_filename(f"portfolio_{seller_id}_{uuid.uuid4().hex}.{ext}")
            path = f"portfolio-images/{seller_id}/{filename}"

            upload_res = supabase.storage.from_("portfolio-images").upload(
                path=path,
                file=file_content,
                file_options={"cacheControl": "3600", "upsert": "true"}
            )

            if upload_res.status_code not in (200, 201):
                logger.warning(f"Portfolio upload failed for {path}")
                continue

            public_url = supabase.storage.from_("portfolio-images").get_public_url(path)
            uploaded_urls.append(public_url)

        except Exception as e:
            logger.error(f"Portfolio image upload error (seller {seller_id}): {str(e)}")
            continue

    if uploaded_urls:
        # Append to existing portfolio_images array
        current = supabase.table("profiles")\
            .select("portfolio_images")\
            .eq("id", seller_id)\
            .maybe_single().execute().data

        existing = current.get("portfolio_images", []) if current else []
        updated = existing + uploaded_urls

        supabase.table("profiles")\
            .update({"portfolio_images": updated, "updated_at": "now()"})\
            .eq("id", seller_id)\
            .execute()

        log_action(
            actor_id=seller_id,
            action="upload_portfolio_images",
            details={"count": len(uploaded_urls), "urls": uploaded_urls}
        )

    return jsonify({
        "message": f"{len(uploaded_urls)} portfolio image(s) uploaded successfully",
        "urls": uploaded_urls
    }), 200


# ────────────────────────────────────────────────
# GET /api/seller/dashboard
# Seller stats (gigs, bookings, earnings, rating)
# ────────────────────────────────────────────────
@bp.route("/dashboard", methods=["GET"])
@jwt_required()
@seller_required
def seller_dashboard():
    seller_id = get_jwt_identity()

    try:
        # Active gigs
        gigs_count = safe_supabase_query(
            supabase.table("gigs")
                .select("id", count="exact")
                .eq("seller_id", seller_id)
                .eq("status", "published")
        ).count or 0

        # Active bookings
        active_bookings = supabase.table("bookings")\
            .select("id", count="exact")\
            .eq("seller_id", seller_id)\
            .in_("status", ["pending", "accepted", "in_progress"])\
            .execute().count or 0

        # Completed bookings
        completed_bookings = supabase.table("bookings")\
            .select("id", count="exact")\
            .eq("seller_id", seller_id)\
            .eq("status", "completed")\
            .execute().count or 0

        # Rating & review count
        reviews_res = supabase.table("reviews")\
            .select("rating")\
            .eq("reviewed_id", seller_id)\
            .execute()

        review_count = len(reviews_res.data or [])
        avg_rating = sum(r["rating"] for r in (reviews_res.data or [])) / review_count if review_count else 0

        # Monthly earnings (current month)
        start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        earnings_res = supabase.table("bookings")\
            .select("price")\
            .eq("seller_id", seller_id)\
            .eq("status", "completed")\
            .gte("created_at", start_of_month.isoformat())\
            .execute()

        monthly_earnings = sum(b["price"] or 0 for b in (earnings_res.data or []))

        return jsonify({
            "activeGigs": gigs_count,
            "activeBookings": active_bookings,
            "completedBookings": completed_bookings,
            "rating": round(avg_rating, 1),
            "reviewCount": review_count,
            "monthlyEarnings": monthly_earnings
        }), 200

    except Exception as e:
        current_app.logger.error(f"Seller dashboard error (seller {seller_id}): {str(e)}")
        return jsonify({"error": "Failed to load dashboard"}), 500


# ────────────────────────────────────────────────
# GET /api/seller/bookings
# List seller's bookings
# ────────────────────────────────────────────────
@bp.route("/bookings", methods=["GET"])
@jwt_required()
@seller_required
def seller_bookings():
    seller_id = get_jwt_identity()

    try:
        res = supabase.table("bookings")\
            .select("""
                id, status, price, requirements, created_at, updated_at,
                gig:gig_id (id, title, price),
                buyer:buyer_id (id, full_name, avatar_url)
            """)\
            .eq("seller_id", seller_id)\
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
                "gig": row.get("gig") or {"title": "Untitled", "price": row["price"]},
                "buyer": row.get("buyer") or {"full_name": "Unknown", "avatar_url": None}
            })

        return jsonify(bookings), 200

    except Exception as e:
        current_app.logger.error(f"Seller bookings error (seller {seller_id}): {str(e)}")
        return jsonify({"error": "Failed to load bookings"}), 500


# ────────────────────────────────────────────────
# PATCH /api/seller/bookings/:id/status
# Update booking status (accept/reject pending only)
# ────────────────────────────────────────────────
@bp.route("/bookings/<string:id>/status", methods=["PATCH"])
@jwt_required()
@seller_required
def update_booking_status(id):
    seller_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    new_status = data.get("status")

    if new_status not in ["accepted", "rejected"]:
        return jsonify({"error": "Invalid status. Must be 'accepted' or 'rejected'"}), 400

    try:
        booking = supabase.table("bookings")\
            .select("id, status, seller_id")\
            .eq("id", id)\
            .maybe_single().execute().data

        if not booking:
            return jsonify({"error": "Booking not found"}), 404

        if booking["seller_id"] != seller_id:
            return jsonify({"error": "Unauthorized"}), 403

        if booking["status"] != "pending":
            return jsonify({"error": "Only pending bookings can be updated"}), 400

        supabase.table("bookings")\
            .update({
                "status": new_status,
                "updated_at": "now()"
            })\
            .eq("id", id)\
            .execute()

        log_action(
            actor_id=seller_id,
            action=f"booking_{new_status}",
            target_id=id
        )

        return jsonify({"message": f"Booking {new_status} successfully"}), 200

    except Exception as e:
        current_app.logger.error(f"Booking status update error (seller {seller_id}, booking {id}): {str(e)}")
        return jsonify({"error": "Failed to update booking status"}), 500


# ────────────────────────────────────────────────
# GET /api/seller/profile
# Get seller's own profile
# ────────────────────────────────────────────────
@bp.route("/profile", methods=["GET"])
@jwt_required()
@seller_required
def get_seller_profile():
    seller_id = get_jwt_identity()

    try:
        profile_res = supabase.table("profiles")\
            .select("id, full_name, phone, email, role, avatar_url, bio, created_at, updated_at")\
            .eq("id", seller_id)\
            .maybe_single().execute()

        if not profile_res.data:
            return jsonify({"error": "Profile not found"}), 404

        profile = profile_res.data

        reviews_res = supabase.table("reviews")\
            .select("rating")\
            .eq("reviewed_id", seller_id)\
            .execute()

        review_count = len(reviews_res.data or [])
        avg_rating = sum(r["rating"] for r in (reviews_res.data or [])) / review_count if review_count else 0.0

        return jsonify({
            **profile,
            "average_rating": round(avg_rating, 1),
            "review_count": review_count
        }), 200

    except Exception as e:
        current_app.logger.exception(f"Seller profile fetch error (seller {seller_id})")
        return jsonify({"error": "Failed to load profile"}), 500


# ────────────────────────────────────────────────
# PATCH /api/seller/profile
# Update allowed profile fields
# ────────────────────────────────────────────────
@bp.route("/profile", methods=["PATCH"])
@jwt_required()
@seller_required
def update_seller_profile():
    seller_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    allowed_fields = ["full_name", "phone", "bio", "avatar_url"]
    update_data = {k: v for k, v in data.items() if k in allowed_fields and v is not None}

    if not update_data:
        return jsonify({"error": "No valid fields to update"}), 400

    # Validation
    if "full_name" in update_data:
        name = str(update_data["full_name"]).strip()
        if len(name) < 2 or len(name) > 100:
            return jsonify({"error": "Full name must be 2–100 characters"}), 400
        update_data["full_name"] = name

    if "phone" in update_data:
        phone = str(update_data["phone"]).strip()
        update_data["phone"] = phone if phone else None

    if "bio" in update_data:
        bio = str(update_data["bio"]).strip()
        if len(bio) > 1000:
            return jsonify({"error": "Bio cannot exceed 1000 characters"}), 400
        update_data["bio"] = bio if bio else None

    try:
        res = supabase.table("profiles")\
            .update({**update_data, "updated_at": "now()"})\
            .eq("id", seller_id)\
            .execute()

        if not res.data:
            return jsonify({"error": "Profile not found or update failed"}), 404

        log_action(
            actor_id=seller_id,
            action="update_seller_profile",
            details={"updated_fields": list(update_data.keys())}
        )

        return jsonify({"message": "Profile updated successfully"}), 200

    except Exception as e:
        current_app.logger.exception(f"Seller profile update error (seller {seller_id})")
        return jsonify({"error": "Failed to update profile"}), 500


# ────────────────────────────────────────────────
# GET /api/seller/verification
# Get seller's verification status
# ────────────────────────────────────────────────
@bp.route("/verification", methods=["GET"])
@jwt_required()
@seller_required
def get_verification():
    seller_id = get_jwt_identity()

    try:
        res = supabase.table("verifications")\
            .select("id, status, evidence_urls, submitted_at, rejection_reason, reviewed_by, reviewed_at")\
            .eq("seller_id", seller_id)\
            .order("submitted_at", desc=True)\
            .limit(1)\
            .maybe_single()\
            .execute()

        if not res.data:
            return jsonify(None), 200

        return jsonify(res.data), 200

    except Exception as e:
        logger.exception(f"Verification fetch failed for seller {seller_id}")
        return jsonify({"error": "Failed to load verification"}), 500


# ────────────────────────────────────────────────
# POST /api/seller/verification
# Submit verification documents
# ────────────────────────────────────────────────
@bp.route("/verification", methods=["POST"])
@jwt_required()
@seller_required
def submit_verification():
    seller_id = get_jwt_identity()

    if "files" not in request.files:
        return jsonify({"error": "No files provided"}), 400

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files selected"}), 400

    evidence_urls = []
    bucket = "verifications"

    try:
        for file in files:
            if file.filename == "" or not allowed_file(file.filename):
                continue

            filename = secure_filename(f"{seller_id}_{uuid.uuid4().hex}_{file.filename}")
            path = f"{seller_id}/{filename}"

            upload_res = supabase.storage.from_(bucket).upload(
                path=path,
                file=file.read(),
                file_options={"cacheControl": "3600", "upsert": "true"}
            )

            if upload_res.status_code not in (200, 201):
                logger.warning(f"Verification upload failed: {upload_res}")
                continue

            public_url = supabase.storage.from_(bucket).get_public_url(path)
            evidence_urls.append(public_url)

        if not evidence_urls:
            return jsonify({"error": "No valid files uploaded"}), 400

        verification = {
            "seller_id": seller_id,
            "status": "pending",
            "evidence_urls": evidence_urls,
            "submitted_at": "now()",
            "created_at": "now()",
            "updated_at": "now()"
        }

        res = supabase.table("verifications").insert(verification).execute()

        if not res.data:
            return jsonify({"error": "Failed to submit verification"}), 500

        log_action(
            actor_id=seller_id,
            action="submit_verification",
            details={"file_count": len(evidence_urls)}
        )

        return jsonify({
            "message": "Verification submitted successfully",
            "verification_id": res.data[0]["id"]
        }), 201

    except Exception as e:
        logger.exception(f"Verification submission failed for seller {seller_id}")
        return jsonify({"error": "Failed to submit verification"}), 500


# ────────────────────────────────────────────────
# PATCH /api/seller/bookings/:id/cancel
# Seller cancel booking
# ────────────────────────────────────────────────
@bp.route("/bookings/<string:id>/cancel", methods=["PATCH"])
@jwt_required()
@seller_required
def seller_cancel_booking(id):
    seller_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "").strip()

    if len(reason) < 10:
        return jsonify({"error": "Cancellation reason must be at least 10 characters"}), 400

    try:
        booking = supabase.table("bookings")\
            .select("id, status, seller_id")\
            .eq("id", id)\
            .maybe_single().execute().data

        if not booking:
            return jsonify({"error": "Booking not found"}), 404

        if booking["seller_id"] != seller_id:
            return jsonify({"error": "Unauthorized"}), 403

        if booking["status"] in ["completed", "cancelled"]:
            return jsonify({"error": "Booking cannot be cancelled in this status"}), 400

        supabase.table("bookings")\
            .update({
                "status": "cancelled",
                "cancel_reason": f"Seller: {reason}",
                "updated_at": "now()"
            })\
            .eq("id", id)\
            .execute()

        log_action(
            actor_id=seller_id,
            action="cancel_booking",
            target_id=id,
            details={"reason": reason}
        )

        return jsonify({"message": "Booking cancelled by seller"}), 200

    except Exception as e:
        current_app.logger.error(f"Seller cancel booking error (seller {seller_id}, booking {id}): {str(e)}")
        return jsonify({"error": "Failed to cancel booking"}), 500


# ────────────────────────────────────────────────
# POST /api/seller/payout/request
# Request payout
# ────────────────────────────────────────────────
@bp.route("/payout/request", methods=["POST"])
@jwt_required()
@seller_required
def request_payout():
    seller_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    amount = data.get("amount")

    if not isinstance(amount, (int, float)) or amount <= 0:
        return jsonify({"error": "Invalid amount"}), 400

    try:
        # Check available earnings
        earnings = supabase.table("bookings")\
            .select("price")\
            .eq("seller_id", seller_id)\
            .eq("status", "completed")\
            .execute().data or []

        total_earned = sum(b["price"] or 0 for b in earnings)

        if amount > total_earned:
            return jsonify({"error": "Requested amount exceeds available earnings"}), 400

        payout = {
            "seller_id": seller_id,
            "amount": amount,
            "status": "pending",
            "requested_at": "now()"
        }

        res = supabase.table("payouts").insert(payout).execute()

        if not res.data:
            return jsonify({"error": "Failed to request payout"}), 500

        log_action(
            actor_id=seller_id,
            action="request_payout",
            details={"amount": amount}
        )

        return jsonify({
            "message": "Payout request submitted",
            "payout_id": res.data[0]["id"]
        }), 201

    except Exception as e:
        current_app.logger.error(f"Payout request error (seller {seller_id}): {str(e)}")
        return jsonify({"error": "Failed to request payout"}), 500


# ────────────────────────────────────────────────
# AVAILABILITY ROUTES
# ────────────────────────────────────────────────

@bp.route("/availability", methods=["GET"])
@jwt_required()
@seller_required
def list_availability():
    user_id = get_jwt_identity()
    try:
        slots = supabase.table("seller_availability")\
            .select("*")\
            .eq("seller_id", user_id)\
            .order("start_time")\
            .execute().data or []
        return jsonify(slots), 200

    except Exception as e:
        logger.error(f"Availability list failed: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to load availability"}), 500


@bp.route("/availability", methods=["POST"])
@jwt_required()
@seller_required
def create_availability():
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    required = ["start_time", "end_time"]
    if not all(k in data for k in required):
        return jsonify({"error": "start_time and end_time required"}), 400

    try:
        start = data["start_time"]
        end = data["end_time"]

        if end <= start:
            return jsonify({"error": "end_time must be after start_time"}), 400

        notes = data.get("notes")
        if notes is not None:
            notes = str(notes).strip() or None

        slot = {
            "seller_id": user_id,
            "start_time": start,
            "end_time": end,
            "notes": notes,
            "is_booked": False,
            "created_at": "now()",
            "updated_at": "now()"
        }

        inserted = supabase.table("seller_availability").insert(slot).execute()

        if not inserted.data:
            return jsonify({"error": "Failed to create slot"}), 500

        log_action(
            actor_id=user_id,
            action="create_availability_slot",
            details={"start": start, "end": end}
        )

        return jsonify(inserted.data[0]), 201

    except Exception as e:
        logger.error(f"Create availability failed: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to add slot"}), 500


@bp.route("/availability/<slot_id>", methods=["DELETE"])
@jwt_required()
@seller_required
def delete_availability(slot_id):
    user_id = get_jwt_identity()

    try:
        slot = supabase.table("seller_availability")\
            .select("seller_id, is_booked")\
            .eq("id", slot_id)\
            .maybe_single()\
            .execute().data

        if not slot:
            return jsonify({"error": "Slot not found"}), 404

        if slot["seller_id"] != user_id:
            return jsonify({"error": "Not your slot"}), 403

        if slot["is_booked"]:
            return jsonify({"error": "Cannot delete booked slot"}), 400

        supabase.table("seller_availability").delete().eq("id", slot_id).execute()

        log_action(
            actor_id=user_id,
            action="delete_availability_slot",
            target_id=slot_id
        )

        return jsonify({"message": "Slot deleted"}), 200

    except Exception as e:
        logger.error(f"Delete availability failed: {str(e)}")
        return jsonify({"error": "Failed to delete slot"}), 500


# ────────────────────────────────────────────────
# OFFERS & RESPONSES
# ────────────────────────────────────────────────

@bp.route("/offers", methods=["GET"])
@jwt_required()
@seller_required
def get_my_offers():
    user_id = get_jwt_identity()
    try:
        offers = supabase.table("service_offers")\
            .select("*, job_requests!request_id (title, description, budget, category)")\
            .eq("seller_id", user_id)\
            .eq("status", "pending")\
            .order("created_at", desc=True)\
            .execute().data or []
        return jsonify(offers), 200
    except Exception as e:
        logger.error(f"Get offers failed: {str(e)}")
        return jsonify({"error": "Failed to load offers"}), 500


@bp.route("/offers/<offer_id>/respond", methods=["PATCH"])
@jwt_required()
@seller_required
def respond_to_offer(offer_id):
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    action = data.get("action")  # "accept" or "reject"

    if action not in ["accept", "reject"]:
        return jsonify({"error": "action must be 'accept' or 'reject'"}), 400

    try:
        offer = supabase.table("service_offers")\
            .select("id, request_id, status, seller_id")\
            .eq("id", offer_id)\
            .maybe_single()\
            .execute().data

        if not offer or offer["seller_id"] != user_id:
            return jsonify({"error": "Offer not found or not yours"}), 404

        if offer["status"] != "pending":
            return jsonify({"error": "Offer already processed"}), 400

        supabase.table("service_offers")\
            .update({"status": "accepted" if action == "accept" else "rejected"})\
            .eq("id", offer_id)\
            .execute()

        if action == "accept":
            request_data = supabase.table("job_requests")\
                .select("buyer_id, category, title, budget")\
                .eq("id", offer["request_id"])\
                .single()\
                .execute().data

            if not request_data:
                return jsonify({"error": "Associated request not found"}), 404

            booking = {
                "buyer_id": request_data["buyer_id"],
                "seller_id": user_id,
                "gig_id": None,  # no gig for custom jobs
                "status": "accepted",
                "price": request_data["budget"],
                "service": request_data["title"],
                "created_at": "now()",
                "updated_at": "now()"
            }

            booking_res = supabase.table("bookings").insert(booking).execute()

            supabase.table("job_requests")\
                .update({"status": "accepted"})\
                .eq("id", offer["request_id"])\
                .execute()

            log_action(
                actor_id=user_id,
                action="accept_service_offer",
                target_id=offer_id,
                details={"booking_id": booking_res.data[0]["id"]}
            )

            return jsonify({
                "message": "Offer accepted – booking created",
                "booking_id": booking_res.data[0]["id"]
            }), 200

        else:
            supabase.table("job_requests")\
                .update({"status": "rejected"})\
                .eq("id", offer["request_id"])\
                .execute()

            log_action(
                actor_id=user_id,
                action="reject_service_offer",
                target_id=offer_id
            )

            return jsonify({"message": "Offer rejected"}), 200

    except Exception as e:
        logger.error(f"Respond to offer failed: {str(e)}")
        return jsonify({"error": "Server error"}), 500


# ────────────────────────────────────────────────
# CONVERSATIONS & MESSAGES
# ────────────────────────────────────────────────

@bp.route("/conversations", methods=["GET"])
@jwt_required()
@seller_required
def get_seller_conversations():
    seller_id = get_jwt_identity()

    try:
        res = supabase.table("messages")\
            .select("""
                id,
                sender_id,
                receiver_id,
                content,
                created_at,
                sender:profiles!sender_id (full_name, avatar_url),
                receiver:profiles!receiver_id (full_name, avatar_url)
            """)\
            .or_(f"sender_id.eq.{seller_id},receiver_id.eq.{seller_id}")\
            .order("created_at", desc=True)\
            .execute()

        conversations = []
        seen = set()

        for msg in (res.data or []):
            other_id = msg["sender_id"] if msg["receiver_id"] == seller_id else msg["receiver_id"]
            if other_id in seen:
                continue
            seen.add(other_id)

            other_profile = msg["sender"] if msg["receiver_id"] == seller_id else msg["receiver"]
            conversations.append({
                "id": other_id,
                "client_name": other_profile.get("full_name") or "Unknown",
                "client_avatar": other_profile.get("avatar_url"),
                "last_message": msg["content"],
                "last_message_time": msg["created_at"],
                "unread_count": 0,  # TODO: implement later
                "status": "active"
            })

        return jsonify(conversations), 200

    except Exception as e:
        logger.exception(f"Seller conversations fetch failed for {seller_id}")
        return jsonify({"error": "Failed to load conversations"}), 500


# ────────────────────────────────────────────────
# GET /api/seller/messages/conversation/<conversation_id>
# Get message history
# ────────────────────────────────────────────────
@bp.route("/messages/conversation/<string:conversation_id>", methods=["GET"])
@jwt_required()
@seller_required
def get_seller_chat_history(conversation_id):
    seller_id = get_jwt_identity()
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)

    try:
        # Basic authorization check: seller must be a participant
        participant_check = supabase.table("messages")\
            .select("id", count="exact")\
            .or_(f"sender_id.eq.{seller_id},receiver_id.eq.{seller_id}")\
            .eq("booking_id", conversation_id)\
            .limit(1)\
            .execute()

        if participant_check.count == 0:
            return jsonify({"error": "Conversation not found or you are not part of it"}), 403

        # Fetch message history
        res = supabase.table("messages")\
            .select("""
                id,
                sender_id,
                receiver_id,
                content,
                created_at,
                read_at,
                is_file,
                file_url,
                mime_type,
                duration,
                sender:profiles!sender_id (full_name, avatar_url),
                receiver:profiles!receiver_id (full_name, avatar_url)
            """)\
            .eq("booking_id", conversation_id)\
            .or_(f"sender_id.eq.{seller_id},receiver_id.eq.{seller_id}")\
            .order("created_at", asc=True)\
            .range(offset, offset + limit - 1)\
            .execute()

        messages = []
        for msg in (res.data or []):
            sender = msg.pop("sender", {}) or {}
            receiver = msg.pop("receiver", {}) or {}

            messages.append({
                **msg,
                "sender_name": sender.get("full_name", "Unknown"),
                "sender_avatar": sender.get("avatar_url"),
                "receiver_name": receiver.get("full_name", "Unknown"),
                "receiver_avatar": receiver.get("avatar_url"),
                "is_sent_by_me": msg["sender_id"] == seller_id
            })

        # Mark messages as read (only those sent to seller)
        unread = supabase.table("messages")\
            .update({"read_at": "now()"})\
            .eq("receiver_id", seller_id)\
            .eq("booking_id", conversation_id)\
            .is_("read_at", None)\
            .execute()

        if unread.data:
            log_action(
                actor_id=seller_id,
                action="mark_messages_read",
                details={"conversation_id": conversation_id, "count": len(unread.data)}
            )

        return jsonify({
            "messages": messages,
            "total": res.count or 0,
            "has_more": len(messages) == limit
        }), 200

    except Exception as e:
        logger.exception(f"Failed to fetch seller chat history: {conversation_id} - {str(e)}")
        return jsonify({"error": "Failed to load conversation"}), 500


# ────────────────────────────────────────────────
# POST /api/seller/messages
# Send a message in a conversation
# ────────────────────────────────────────────────
@bp.route("/messages", methods=["POST"])
@jwt_required()
@seller_required
def send_seller_message():
    seller_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    required = ["receiver_id", "content"]
    for field in required:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    receiver_id = data["receiver_id"]
    content = data["content"].strip()
    booking_id = data.get("booking_id")  # optional - link to booking

    if len(content) < 1 or len(content) > 2000:
        return jsonify({"error": "Message must be 1–2000 characters"}), 400

    try:
        message = {
            "sender_id": seller_id,
            "receiver_id": receiver_id,
            "content": content,
            "booking_id": booking_id or None,
            "created_at": "now()",
            "read_at": None
        }

        res = supabase.table("messages").insert(message).execute()

        if not res.data:
            return jsonify({"error": "Failed to send message"}), 500

        saved_message = res.data[0]

        # Notify receiver via Socket.IO
        socketio.emit("new_message", {
            "id": saved_message["id"],
            "sender_id": seller_id,
            "receiver_id": receiver_id,
            "content": content,
            "booking_id": booking_id,
            "created_at": saved_message["created_at"]
        }, room=f"user_{receiver_id}")

        log_action(
            actor_id=seller_id,
            action="send_message",
            details={"to": receiver_id, "booking_id": booking_id}
        )

        return jsonify({
            "message": "Message sent",
            "sent_message": saved_message
        }), 201

    except Exception as e:
        logger.error(f"Send message failed (seller {seller_id}): {str(e)}")
        return jsonify({"error": "Failed to send message"}), 500


# ────────────────────────────────────────────────
# PATCH /api/seller/messages/:message_id/read
# Mark message(s) as read
# ────────────────────────────────────────────────
@bp.route("/messages/<string:message_id>/read", methods=["PATCH"])
@jwt_required()
@seller_required
def mark_message_read(message_id):
    seller_id = get_jwt_identity()

    try:
        # Mark single message or all in conversation
        query = supabase.table("messages")\
            .update({"read_at": "now()"})\
            .eq("receiver_id", seller_id)\
            .is_("read_at", None)

        # If message_id provided, limit to that message
        if message_id != "all":
            query = query.eq("id", message_id)

        res = query.execute()

        if not res.data:
            return jsonify({"message": "No unread messages found"}), 200

        log_action(
            actor_id=seller_id,
            action="mark_messages_read",
            details={"count": len(res.data), "message_id": message_id}
        )

        return jsonify({
            "message": f"{len(res.data)} message(s) marked as read"
        }), 200

    except Exception as e:
        logger.error(f"Mark read failed (seller {seller_id}): {str(e)}")
        return jsonify({"error": "Failed to mark messages as read"}), 500


# ────────────────────────────────────────────────
# GET /api/seller/notifications
# Get unread notifications / messages count
# ────────────────────────────────────────────────
@bp.route("/notifications", methods=["GET"])
@jwt_required()
@seller_required
def get_seller_notifications():
    seller_id = get_jwt_identity()

    try:
        # Unread messages count
        unread_messages = supabase.table("messages")\
            .select("id", count="exact")\
            .eq("receiver_id", seller_id)\
            .is_("read_at", None)\
            .execute().count or 0

        # New offers / verifications / etc. (expand as needed)
        pending_offers = supabase.table("service_offers")\
            .select("id", count="exact")\
            .eq("seller_id", seller_id)\
            .eq("status", "pending")\
            .execute().count or 0

        return jsonify({
            "unread_messages": unread_messages,
            "pending_offers": pending_offers,
            "total_unread": unread_messages + pending_offers
        }), 200

    except Exception as e:
        logger.error(f"Notifications fetch failed (seller {seller_id}): {str(e)}")
        return jsonify({"error": "Failed to load notifications"}), 500
    

# ────────────────────────────────────────────────
# GET /api/seller/<user_id>/reviews   
# Public endpoint: Get all reviews for a user (seller or buyer)
@bp.route("/profile/<string:user_id>/reviews", methods=["GET"])
def get_profile_reviews(user_id: str):
    """
    Public endpoint: Get reviews for any user (usually sellers)
    Uses explicit join hint to avoid schema cache issues
    """
    try:
        # Use explicit join hint — most reliable after schema cache refresh
        res = supabase.table("reviews")\
            .select("""
                id,
                rating,
                comment,
                created_at,
                reviewer:profiles!reviews_reviewer_id_fkey (full_name, avatar_url)
            """)\
            .eq("reviewed_id", user_id)\
            .order("created_at", desc=True)\
            .execute()

        reviews = res.data or []

        # Safe fallback for reviewer data
        for review in reviews:
            reviewer = review.get("reviewer") or {}
            review["reviewer"] = {
                "full_name": reviewer.get("full_name", "Anonymous"),
                "avatar_url": reviewer.get("avatar_url")
            }

        # Calculate average rating safely
        if reviews:
            total_rating = sum(r["rating"] for r in reviews if isinstance(r["rating"], (int, float)))
            avg_rating = round(total_rating / len(reviews), 1)
        else:
            avg_rating = None

        return jsonify({
            "reviews": reviews,
            "average_rating": avg_rating,
            "total_reviews": len(reviews)
        }), 200

    except postgrest.exceptions.APIError as e:
        # Handle schema cache / relationship not found
        if e.code == "PGRST200":
            logger.warning(f"Relationship hint failed for reviews (user {user_id}) - using fallback")
            
            fallback = supabase.table("reviews")\
                .select("id, rating, comment, created_at")\
                .eq("reviewed_id", user_id)\
                .order("created_at", desc=True)\
                .execute()

            return jsonify({
                "reviews": fallback.data or [],
                "average_rating": None,
                "total_reviews": len(fallback.data or []),
                "warning": "Reviewer names/avatars unavailable (schema cache issue - restart Supabase project to fix)"
            }), 200

        logger.exception(f"PostgREST error fetching reviews for user {user_id}: {str(e)}")
        return jsonify({"error": "Failed to load reviews"}), 500

    except Exception as e:
        logger.exception(f"Unexpected error fetching reviews for user {user_id}: {str(e)}")
        return jsonify({"error": "Internal error loading reviews"}), 500
    