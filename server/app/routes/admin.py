# app/routes/admin.py
from flask import Blueprint, app, current_app, json, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import postgrest
import socketio
from app.services.supabase_service import supabase
from app.utils.decorators import admin_required
from datetime import datetime
from typing import Dict, Any
import uuid, logging
from app.extensions import limiter
from app.utils.utils import broadcast_log

bp = Blueprint("admin", __name__, url_prefix="/api/admin")

logger = logging.getLogger(__name__)

# =============================================================================
# HELPERS
# =============================================================================

def parse_pagination() -> Dict[str, int]:
    """Extract safe pagination params"""
    try:
        page = max(int(request.args.get("page", 1)), 1)
        per_page = min(max(int(request.args.get("per_page", 20)), 1), 100)
    except (TypeError, ValueError):
        page, per_page = 1, 20
    return {"page": page, "per_page": per_page}


def build_query_with_filters(
    table: str,
    filters: Dict[str, Any],
    order_by: str = "created_at"
):
    """Build paginated & filtered Supabase query"""
    query = supabase.table(table).select("*")

    for key, value in filters.items():
        if value is not None:
            if isinstance(value, bool):
                query = query.eq(key, value)
            elif isinstance(value, str):
                query = query.ilike(key, f"%{value}%")
            else:
                query = query.eq(key, value)

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    from_idx = (page - 1) * per_page
    to_idx = from_idx + per_page - 1

    # Keep your existing hard-coded desc=True (newest first)
    query = query.range(from_idx, to_idx).order(order_by, desc=True)

    # Count query for total
    count_query = supabase.table(table).select("count", count="exact")
    for key, value in filters.items():
        if value is not None:
            count_query = count_query.eq(key, value)

    count_result = count_query.execute()
    # Safe count access (handles older/newer supabase-py versions)
    total = count_result.count if hasattr(count_result, "count") else 0

    return query, {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page if per_page else 1,
        "has_more": (page * per_page) < total
    }

# =============================================================================
# Helper: Modern supabase response handler
# =============================================================================
def handle_supabase_response(response):
    """
    Safe handler for supabase-py v2+ responses.
    Returns data or raises exception.
    """
    if not response:
        raise ValueError("No response from Supabase")

    return response.data or []


def log_admin_action(action: str, target_id: str, details: Dict = None):
    """Log admin actions to audit_logs"""
    try:
        log_entry = {
            "user_id": get_jwt_identity(),
            "action": action,
            "details": details or {},
            "created_at": datetime.utcnow().isoformat()
        }
        supabase.table("audit_logs").insert(log_entry).execute()
    except Exception as e:
        logger.error(f"Audit log failed: {str(e)}")


# =============================================================================
# SUPPORT TICKETS
# =============================================================================

@bp.route("/tickets", methods=["GET"])
@jwt_required()
@admin_required
@limiter.limit("50 per minute")
def list_tickets():
    try:
        filters = {
            "status": request.args.get("status"),
            "user_id": request.args.get("user_id"),
            "priority": request.args.get("priority")
        }
        query, page_info = build_query_with_filters("support_tickets", filters, order_by="created_at")
        tickets = handle_supabase_response(query.execute()) or []

        return jsonify({"tickets": tickets, **page_info}), 200

    except Exception as e:
        logger.error("Failed to list tickets", exc_info=e)
        return jsonify({"error": "Failed to fetch tickets"}), 500


@bp.route("/tickets", methods=["POST"])
@jwt_required()
@admin_required
@limiter.limit("20 per minute")
def create_ticket():
    data: Dict[str, Any] = request.get_json(silent=True) or {}
    required = ["user_id", "subject", "description"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    try:
        ticket = {
            "user_id": data["user_id"],
            "subject": data["subject"].strip(),
            "description": data["description"].strip(),
            "status": data.get("status", "open"),
            "priority": data.get("priority", "medium"),
            "assigned_to": data.get("assigned_to"),
            "created_at": "now()",
            "status_history": [{
                "status": "open",
                "changed_at": datetime.utcnow().isoformat(),
                "changed_by": "admin",
                "reason": data.get("initial_reason", "Created by admin")
            }]
        }

        resp = supabase.table("support_tickets").insert(ticket).execute()
        result = handle_supabase_response(resp, single=True)
        if not result:
            return jsonify({"error": "Failed to create ticket"}), 500

        log_admin_action(
            action="create_ticket",
            target_id=result[0]["id"],
            details={"user_id": data["user_id"], "subject": data["subject"]}
        )

        return jsonify(result[0]), 201

    except Exception as e:
        logger.error("Ticket creation failed", exc_info=e)
        return jsonify({"error": "Creation failed"}), 500


@bp.route("/tickets/<ticket_id>", methods=["GET"])
@jwt_required()
@admin_required
def get_ticket(ticket_id: str):
    try:
        resp = supabase.table("support_tickets").select("*").eq("id", ticket_id).maybe_single().execute()
        ticket = handle_supabase_response(resp, single=True)
        if not ticket:
            return jsonify({"error": "Ticket not found"}), 404
        return jsonify(ticket), 200

    except Exception as e:
        logger.error(f"Get ticket failed (ticket_id: {ticket_id})", exc_info=e)
        return jsonify({"error": "Failed to fetch ticket"}), 500


@bp.route("/tickets/<ticket_id>", methods=["PATCH"])
@jwt_required()
@admin_required
@limiter.limit("30 per minute")
def update_ticket(ticket_id: str):
    data: Dict[str, Any] = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No update data provided"}), 400

    try:
        resp = supabase.table("support_tickets")\
            .select("status, status_history")\
            .eq("id", ticket_id)\
            .maybe_single().execute()

        current = handle_supabase_response(resp, single=True)
        if not current:
            return jsonify({"error": "Ticket not found"}), 404

        update_data: Dict[str, Any] = {}
        status_history = current.get("status_history", []) or []

        # Status change with history
        if "status" in data and data["status"] != current["status"]:
            reason = data.get("reason", "").strip()
            if not reason:
                return jsonify({"error": "Reason required when changing status"}), 400

            update_data["status"] = data["status"]
            status_history.append({
                "status": data["status"],
                "changed_at": datetime.utcnow().isoformat(),
                "changed_by": "admin",
                "reason": reason
            })
            update_data["status_history"] = status_history

        # Other updatable fields
        allowed = ["subject", "description", "priority", "assigned_to", "resolution_notes"]
        for field in allowed:
            if field in data:
                update_data[field] = data[field]

        if update_data:
            update_data["updated_at"] = "now()"
            resp = supabase.table("support_tickets")\
                .update(update_data)\
                .eq("id", ticket_id)\
                .execute()

            updated = handle_supabase_response(resp, single=True)
            if not updated:
                return jsonify({"error": "Update failed"}), 400

            log_admin_action(
                action="update_ticket",
                target_id=ticket_id,
                details={"changed_fields": list(update_data.keys())}
            )

            return jsonify(updated[0]), 200

        return jsonify({"message": "No changes applied"}), 200

    except Exception as e:
        logger.error(f"Ticket update failed (ticket_id: {ticket_id})", exc_info=e)
        return jsonify({"error": "Update failed"}), 500


@bp.route("/tickets/<ticket_id>", methods=["DELETE"])
@jwt_required()
@admin_required
@limiter.limit("5 per minute")
def delete_ticket(ticket_id: str):
    try:
        resp = supabase.table("support_tickets").delete().eq("id", ticket_id).execute()
        deleted = handle_supabase_response(resp, single=True)
        if not deleted:
            return jsonify({"error": "Ticket not found"}), 404

        log_admin_action(
            action="delete_ticket",
            target_id=ticket_id,
            details={"deleted_by": get_jwt_identity()}
        )

        return jsonify({"message": "Ticket deleted"}), 200

    except Exception as e:
        logger.error(f"Ticket delete failed (ticket_id: {ticket_id})", exc_info=e)
        return jsonify({"error": "Delete failed"}), 500


# =============================================================================
# USERS – List all users (paginated, filterable)
# GET /api/admin/users
# =============================================================================
@bp.route("/users", methods=["GET"])
@jwt_required()
@admin_required
@limiter.limit("50 per minute")
def list_users():
    """
    Admin: List users with pagination, search, and role filter
    Query params: page, limit, search, role
    """
    try:
        page = request.args.get("page", 1, type=int)
        limit = request.args.get("limit", 10, type=int)
        search = request.args.get("search", "").strip()
        role = request.args.get("role", "all")

        offset = (page - 1) * limit

        query = supabase.table("profiles")\
            .select("""
                id,
                full_name,
                email,
                role,
                created_at,
                is_verified,
                is_online,
                rating,
                review_count,
                banned,
                evidence_url
            """, count="exact")\
            .order("created_at", desc=True)

        if search:
            query = query.or_(
                f"full_name.ilike.%{search}%,email.ilike.%{search}%,id.ilike.%{search}%"
            )

        if role != "all":
            query = query.eq("role", role)

        res = query.range(offset, offset + limit - 1).execute()

        return jsonify({
            "users": res.data or [],
            "total": res.count or 0,
            "page": page,
            "limit": limit,
            "total_pages": ((res.count or 0) + limit - 1) // limit if limit > 0 else 1
        }), 200

    except Exception as e:
        current_app.logger.exception("Admin users list failed")
        return jsonify({"error": "Failed to fetch users"}), 500


# =============================================================================
# USERS – Update single user (ban/unban/verify/unverify)
# PATCH /api/admin/users/:user_id
# =============================================================================
@bp.route("/users/<string:user_id>", methods=["PATCH"])
@jwt_required()
@admin_required
@limiter.limit("15 per minute")
def update_user(user_id: str):
    """
    Admin: Update user status
    Body: { "action": "ban" | "unban" | "verify" | "unverify" }
    """
    data = request.get_json(silent=True) or {}
    action = data.get("action")

    if action not in ["ban", "unban", "verify", "unverify"]:
        return jsonify({"error": "Invalid action"}), 400

    field = "banned" if action in ["ban", "unban"] else "is_verified"
    value = action in ["ban", "verify"]

    try:
        res = supabase.table("profiles")\
            .update({field: value, "updated_at": "now()"})\
            .eq("id", user_id)\
            .execute()

        if not res.data:
            return jsonify({"error": "User not found"}), 404

        # Audit log
        supabase.table("audit_logs").insert({
            "user_id": user_id,
            "action": f"admin_{action}",
            "details": {
                "admin_id": get_jwt_identity(),
                "field": field,
                "value": value,
                "timestamp": datetime.utcnow().isoformat()
            }
        }).execute()

        return jsonify({"message": f"User {action}ed successfully"}), 200

    except Exception as e:
        current_app.logger.exception(f"User update failed: {user_id}")
        return jsonify({"error": "Update failed"}), 500


# =============================================================================
# USERS – Bulk update
# PATCH /api/admin/users/bulk
# =============================================================================
@bp.route("/users/bulk", methods=["PATCH"])
@jwt_required()
@admin_required
@limiter.limit("10 per minute")
def bulk_user_update():
    """
    Admin: Bulk ban/unban/verify/unverify users
    Body: { "action": "...", "userIds": [...] }
    """
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    user_ids = data.get("userIds", [])

    if action not in ["ban", "unban", "verify", "unverify"]:
        return jsonify({"error": "Invalid action"}), 400

    if not user_ids:
        return jsonify({"error": "No users selected"}), 400

    field = "banned" if action in ["ban", "unban"] else "is_verified"
    value = action in ["ban", "verify"]

    try:
        supabase.table("profiles")\
            .update({field: value, "updated_at": "now()"})\
            .in_("id", user_ids)\
            .execute()

        # Audit log (single entry for bulk)
        supabase.table("audit_logs").insert({
            "user_id": None,  # bulk action
            "action": f"admin_bulk_{action}",
            "details": {
                "admin_id": get_jwt_identity(),
                "user_ids": user_ids,
                "field": field,
                "value": value
            }
        }).execute()

        return jsonify({"message": f"Bulk {action} applied to {len(user_ids)} users"}), 200

    except Exception as e:
        current_app.logger.exception("Bulk user update failed")
        return jsonify({"error": "Bulk action failed"}), 500


# =============================================================================
# USERS – Delete/Suspend user
# DELETE /api/admin/users/:user_id
# =============================================================================
@bp.route("/users/<string:user_id>", methods=["DELETE"])
@jwt_required()
@admin_required
@limiter.limit("5 per minute")
def delete_user(user_id: str):
    """
    Admin: Soft-delete user (sets banned = true + audit log)
    """
    current_admin_id = get_jwt_identity()

    if current_admin_id == user_id:
        return jsonify({"error": "Cannot delete your own account"}), 403

    try:
        supabase.table("profiles")\
            .update({"banned": True, "updated_at": "now()"})\
            .eq("id", user_id)\
            .execute()

        supabase.table("audit_logs").insert({
            "user_id": user_id,
            "action": "admin_delete_user",
            "details": {"admin_id": current_admin_id}
        }).execute()

        return jsonify({"message": "User suspended/deleted"}), 200

    except Exception as e:
        current_app.logger.exception(f"User delete failed: {user_id}")
        return jsonify({"error": "Delete failed"}), 500


# =============================================================================
# VERIFICATIONS – List pending verifications
# GET /api/admin/verifications/pending
# =============================================================================
@bp.route("/verifications/pending", methods=["GET"])
@jwt_required()
@admin_required
@limiter.limit("30 per minute")
def list_pending_verifications():
    """
    Admin: List all pending seller verification requests
    """
    try:
        res = supabase.table("verifications")\
            .select("""
                id,
                seller_id,
                type,
                status,
                evidence_urls,
                submitted_at,
                rejection_reason,
                reviewed_at,
                reviewed_by,
                seller:seller_id (full_name, email, phone, bio, avatar_url, portfolio_images, average_rating, review_count)
            """)\
            .eq("status", "pending")\
            .order("submitted_at", desc=True)\
            .execute()

        return jsonify(res.data or []), 200

    except Exception as e:
        current_app.logger.exception("Pending verifications fetch failed")
        return jsonify({"error": "Failed to load pending verifications"}), 500


# =============================================================================
# VERIFICATIONS – Get single verification details
# GET /api/admin/verifications/:verification_id
# =============================================================================
@bp.route("/verifications/<string:verification_id>", methods=["GET"])
@jwt_required()
@admin_required
@limiter.limit("20 per minute")
def get_verification(verification_id: str):
    """
    Admin: Get full details of a verification request
    """
    try:
        res = supabase.table("verifications")\
            .select("""
                *,
                seller:seller_id (full_name, email, phone, bio, avatar_url, portfolio_images, average_rating, review_count)
            """)\
            .eq("id", verification_id)\
            .maybe_single()\
            .execute()

        if not res.data:
            return jsonify({"error": "Verification not found"}), 404

        return jsonify(res.data), 200

    except Exception as e:
        current_app.logger.exception(f"Verification fetch failed: {verification_id}")
        return jsonify({"error": "Failed to load verification"}), 500


# =============================================================================
# VERIFICATIONS – Approve verification
# PATCH /api/admin/verifications/:verification_id/approve
# =============================================================================
@bp.route("/verifications/<string:verification_id>/approve", methods=["PATCH"])
@jwt_required()
@admin_required
@limiter.limit("10 per minute")
def approve_verification(verification_id: str):
    """
    Admin: Approve seller verification → set is_verified = true
    """
    admin_id = get_jwt_identity()

    try:
        ver = supabase.table("verifications")\
            .select("seller_id")\
            .eq("id", verification_id)\
            .maybe_single().execute()

        if not ver.data:
            return jsonify({"error": "Verification not found"}), 404

        seller_id = ver.data["seller_id"]

        # Update verification
        supabase.table("verifications")\
            .update({
                "status": "approved",
                "reviewed_by": admin_id,
                "reviewed_at": "now()",
                "updated_at": "now()"
            })\
            .eq("id", verification_id)\
            .execute()

        # Verify seller profile
        supabase.table("profiles")\
            .update({
                "is_verified": True,
                "updated_at": "now()"
            })\
            .eq("id", seller_id)\
            .execute()

        # Audit log
        supabase.table("audit_logs").insert({
            "user_id": seller_id,
            "action": "admin_verify_approved",
            "details": {"admin_id": admin_id}
        }).execute()

        return jsonify({"message": "Seller verified successfully"}), 200

    except Exception as e:
        current_app.logger.exception(f"Approve verification failed: {verification_id}")
        return jsonify({"error": "Approval failed"}), 500


# =============================================================================
# VERIFICATIONS – Reject verification
# PATCH /api/admin/verifications/:verification_id/reject
# =============================================================================
@bp.route("/verifications/<string:verification_id>/reject", methods=["PATCH"])
@jwt_required()
@admin_required
@limiter.limit("10 per minute")
def reject_verification(verification_id: str):
    """
    Admin: Reject verification with reason
    Body: { "rejection_reason": "..." }
    """
    admin_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    reason = data.get("rejection_reason", "").strip()

    if not reason:
        return jsonify({"error": "Rejection reason required"}), 400

    try:
        ver = supabase.table("verifications")\
            .select("seller_id")\
            .eq("id", verification_id)\
            .maybe_single().execute()

        if not ver.data:
            return jsonify({"error": "Verification not found"}), 404

        seller_id = ver.data["seller_id"]

        # Update verification
        supabase.table("verifications")\
            .update({
                "status": "rejected",
                "rejection_reason": reason,
                "reviewed_by": admin_id,
                "reviewed_at": "now()",
                "updated_at": "now()"
            })\
            .eq("id", verification_id)\
            .execute()

        # Audit log
        supabase.table("audit_logs").insert({
            "user_id": seller_id,
            "action": "admin_verify_rejected",
            "details": {"admin_id": admin_id, "reason": reason}
        }).execute()

        return jsonify({"message": "Verification rejected"}), 200

    except Exception as e:
        current_app.logger.exception(f"Reject verification failed: {verification_id}")
        return jsonify({"error": "Rejection failed"}), 500

# =============================================================================
# Manage Gigs
# =============================================================================
@bp.route("/gigs", methods=["GET"])
@jwt_required()
@admin_required
def list_gigs():
    try:
        query = supabase.table("gigs")\
            .select("*, profiles!seller_id (full_name, email)")\
            .order("created_at", desc=True)

        # Optional filters (add more as needed)
        status = request.args.get("status")
        if status:
            query = query.eq("status", status)

        gigs = handle_supabase_response(query.execute())

        return jsonify(gigs), 200

    except Exception as e:
        logger.error(f"List gigs failed: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to load gigs"}), 500


@bp.route("/gigs/<gig_id>/status", methods=["PATCH"])
@jwt_required()
@admin_required
def update_gig_status(gig_id):
    data = request.get_json(silent=True) or {}
    new_status = data.get("status")

    if new_status not in ["active", "rejected"]:
        return jsonify({"error": "Invalid status"}), 400

    try:
        updated = supabase.table("gigs")\
            .update({"status": new_status, "updated_at": "now()"})\
            .eq("id", gig_id)\
            .execute()

        if not updated.data:
            return jsonify({"error": "Gig not found"}), 404

        logger.info(f"Admin updated gig {gig_id} to {new_status}")
        return jsonify({"message": f"Gig status updated to {new_status}"}), 200

    except Exception as e:
        logger.error(f"Update gig status failed: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to update gig"}), 500


# =============================================================================
# BOOKINGS – Admin endpoints
# =============================================================================
@bp.route("/bookings", methods=["GET"])
@jwt_required()
@admin_required
@limiter.limit("30 per minute")
def list_bookings():
    try:
        filters = {
            "status": request.args.get("status"),
            "buyer_id": request.args.get("buyer_id"),
            "seller_id": request.args.get("seller_id")
        }

        # Correct call – no desc= argument
        query, page_info = build_query_with_filters(
            table="bookings",
            filters=filters,
            order_by="created_at",
            # page and per_page are read from request.args inside the function
        )

        bookings = handle_supabase_response(query.execute()) or []

        return jsonify({"bookings": bookings, **page_info}), 200

    except Exception as e:
        logger.error("List bookings failed", exc_info=True)
        # Always return array shape
        return jsonify({
            "bookings": [],
            "total": 0,
            "page": 1,
            "per_page": 20,
            "total_pages": 1,
            "has_more": False
        }), 200

@bp.route("/bookings/<booking_id>", methods=["PATCH"])
@jwt_required()
@admin_required
@limiter.limit("15 per minute")
def update_booking(booking_id: str):
    """
    PATCH /api/admin/bookings/<booking_id>
    Body: { "status": "...", "price": 123, ... }
    """
    data: Dict[str, Any] = request.get_json(silent=True) or {}
    
    # Allowed fields (you can expand this list)
    allowed = ["status", "price", "service", "cancel_reason", "requirements", "notes"]
    update_data = {k: v for k, v in data.items() if k in allowed and v is not None}

    if not update_data:
        return jsonify({"error": "No valid fields provided for update"}), 400

    try:
        # Update with timestamp
        resp = supabase.table("bookings")\
            .update({**update_data, "updated_at": "now()"})\
            .eq("id", booking_id)\
            .execute()

        updated = handle_supabase_response(resp, single=True)
        if not updated:
            return jsonify({"error": "Booking not found or update failed"}), 404

        # Log admin action
        log_admin_action(
            action="update_booking",
            target_id=booking_id,
            details={
                "changed_fields": list(update_data.keys()),
                "new_values": update_data
            }
        )

        # Optional: invalidate cache if you use any
        # queryClient.invalidateQueries(["admin-bookings"]) → frontend side

        return jsonify(updated), 200

    except Exception as e:
        logger.error(f"Booking update failed (booking_id: {booking_id})", exc_info=True)
        return jsonify({
            "error": "Update failed",
            "detail": str(e) if app.debug else None
        }), 500

@bp.route("/bookings/<booking_id>/status", methods=["PATCH"])
@jwt_required()
@admin_required
def update_booking_status(booking_id):
    data = request.get_json(silent=True) or {}
    new_status = data.get("status")

    if not new_status or new_status not in ["pending", "active", "completed", "cancelled"]:
        return jsonify({"error": "Invalid status"}), 400

    try:
        updated = supabase.table("bookings")\
            .update({"status": new_status, "updated_at": "now()"})\
            .eq("id", booking_id)\
            .execute()

        if not updated.data:
            return jsonify({
                "error": "Booking not found",
                "booking_id": booking_id,
                "message": "The booking may have been deleted or never existed."
            }), 404

        logger.info(f"Admin updated booking {booking_id} to {new_status}")

        return jsonify({
            "message": f"Booking status updated to {new_status}",
            "booking": updated.data[0]
        }), 200

    except Exception as e:
        logger.error(f"Update booking status failed: {str(e)}", exc_info=True)
        return jsonify({"error": "Update failed"}), 500

# =============================================================================
# PAYMENTS
# =============================================================================

@bp.route("/payments", methods=["GET"])
@jwt_required()
@admin_required
@limiter.limit("20 per minute")
def list_payments():
    try:
        filters = {"status": request.args.get("status")}
        query, page_info = build_query_with_filters("payments", filters, order_by="created_at")
        payments = handle_supabase_response(query.execute()) or []

        return jsonify({"payments": payments, **page_info}), 200

    except Exception as e:
        logger.error("List payments failed", exc_info=e)
        return jsonify({"error": "Failed to fetch payments"}), 500


@bp.route("/payments/<payment_id>/refund", methods=["PATCH"])
@jwt_required()
@admin_required
@limiter.limit("5 per minute")
def refund_payment(payment_id: str):
    try:
        resp = supabase.table("payments")\
            .update({"status": "refunded", "updated_at": "now()"})\
            .eq("id", payment_id)\
            .execute()

        updated = handle_supabase_response(resp, single=True)
        if not updated:
            return jsonify({"error": "Payment not found"}), 404

        log_admin_action(
            action="refund_payment",
            target_id=payment_id,
            details={"refunded_by": get_jwt_identity()}
        )

        return jsonify(updated), 200

    except Exception as e:
        logger.error(f"Refund failed (payment_id: {payment_id})", exc_info=e)
        return jsonify({"error": "Refund failed"}), 500


# =============================================================================
# ANALYTICS / DASHBOARD
# =============================================================================
@bp.route("/dashboard", methods=["GET"])
@jwt_required()
@admin_required
def admin_dashboard():
    try:
        total_users = supabase.table("profiles").select("count", count="exact").execute().count or 0
        pending_verifs = supabase.table("verifications").select("count", count="exact").eq("status", "pending").execute().count or 0
        open_tickets = supabase.table("support_tickets").select("count", count="exact").eq("status", "open").execute().count or 0
        active_gigs = supabase.table("gigs").select("count", count="exact").eq("status", "published").execute().count or 0

        return jsonify({
            "total_users": total_users,
            "pending_verifications": pending_verifs,
            "open_tickets": open_tickets,
            "active_gigs": active_gigs
        }), 200

    except Exception as e:
        logger.error(f"Dashboard failed: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to load dashboard"}), 500
    
# =============================================================================
# JOB REQUESTS – Admin endpoints
# =============================================================================

@bp.route("/job-requests", methods=["GET"])
@jwt_required()
@admin_required
def list_job_requests():
    """
    GET /api/admin/job-requests?status=pending
    List job requests with buyer details
    """
    status = request.args.get("status", "pending")
    try:
        reqs = supabase.table("job_requests")\
            .select("*, profiles!buyer_id (full_name, email, phone)")\
            .eq("status", status)\
            .order("created_at", desc=True)\
            .execute()

        reqs_data = handle_supabase_response(reqs)

        formatted = []
        for r in reqs_data:
            buyer = r.pop("profiles!buyer_id", {}) or {}
            formatted.append({
                **r,
                "buyer": {
                    "name": buyer.get("full_name", "Unknown"),
                    "email": buyer.get("email"),
                    "phone": buyer.get("phone")
                }
            })

        return jsonify(formatted), 200

    except Exception as e:
        logger.error(f"List requests failed: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to load requests"}), 500


@bp.route("/job-requests/<request_id>", methods=["GET"])
@jwt_required()
@admin_required
def get_job_request(request_id):
    """
    GET /api/admin/job-requests/:request_id
    Get full details of one job request
    """
    try:
        req = supabase.table("job_requests")\
            .select("""
                *,
                profiles!buyer_id (full_name, email, phone, avatar_url)
            """)\
            .eq("id", request_id)\
            .maybe_single()\
            .execute()

        req_data = handle_supabase_response(req)

        if not req_data:
            return jsonify({"error": "Job request not found"}), 404

        buyer = req_data.pop("profiles!buyer_id", {}) or {}
        response = {
            **req_data,
            "buyer": {
                "name": buyer.get("full_name", "Unknown"),
                "email": buyer.get("email"),
                "phone": buyer.get("phone"),
                "avatar_url": buyer.get("avatar_url")
            }
        }

        return jsonify(response), 200

    except Exception as e:
        logger.error(f"Failed to get job request {request_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to fetch job request"}), 500


@bp.route("/job-requests/<request_id>/assign", methods=["PATCH"])
@jwt_required()
@admin_required
def assign_seller_to_job_request(request_id):
    data = request.get_json(silent=True) or {}
    seller_ids = data.get("seller_ids", [])  # now expects array
    notes = data.get("notes", "").strip()

    if not seller_ids or not isinstance(seller_ids, list):
        return jsonify({"error": "seller_ids must be a non-empty list"}), 400

    try:
        # Get request
        req = supabase.table("job_requests")\
            .select("id, status, category, buyer_id")\
            .eq("id", request_id)\
            .maybe_single()\
            .execute().data

        if not req:
            return jsonify({"error": "Job request not found"}), 404

        if req["status"] != "pending":
            return jsonify({"error": f"Request is already {req['status']}"}), 400

        assigned = []
        for seller_id in seller_ids:
            # Validate seller
            seller = supabase.table("profiles")\
                .select("id, role, employee_category, is_available")\
                .eq("id", seller_id)\
                .eq("role", "seller")\
                .maybe_single()\
                .execute().data

            if not seller:
                continue  # skip invalid

            if seller["employee_category"] != req["category"]:
                continue

            if not seller["is_available"]:
                continue

            # Assign
            update = supabase.table("job_requests")\
                .update({
                    "assigned_seller_id": seller_id,  # if single assign → last one wins, or change schema
                    "status": "assigned",
                    "updated_at": "now()"
                })\
                .eq("id", request_id)\
                .execute()

            if update.data:
                assigned.append(seller_id)

            # Notify seller
            socketio.emit(
                "new_job_assignment",
                {
                    "request_id": request_id,
                    "buyer_id": req["buyer_id"],
                    "category": req["category"],
                    "title": req["title"],
                    "notes": notes
                },
                room=seller_id
            )

        if not assigned:
            return jsonify({"error": "No valid sellers could be assigned"}), 400

        return jsonify({
            "message": f"Assigned {len(assigned)} seller(s)",
            "assigned_seller_ids": assigned
        }), 200

    except Exception as e:
        logger.error(f"Assign failed for {request_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to assign seller(s)"}), 500


@bp.route("/job-requests/<request_id>/status", methods=["PATCH"])
@jwt_required()
@admin_required
def update_job_request_status(request_id):
    """
    PATCH /api/admin/job-requests/:request_id/status
    Body: { "status": "cancelled|rejected", "reason": "optional string" }
    """
    data = request.get_json(silent=True) or {}
    new_status = data.get("status")
    reason = data.get("reason", "").strip()

    valid_statuses = ["cancelled", "rejected"]
    if not new_status or new_status not in valid_statuses:
        return jsonify({"error": f"Valid statuses: {', '.join(valid_statuses)}"}), 400

    try:
        updated = supabase.table("job_requests")\
            .update({
                "status": new_status,
                "updated_at": "now()"
            })\
            .eq("id", request_id)\
            .execute()

        if not updated.data:
            return jsonify({"error": "Job request not found"}), 404

        logger.info(f"Admin updated job request {request_id} to {new_status} (reason: {reason})")

        return jsonify({
            "message": f"Request marked as {new_status}",
            "request_id": request_id,
            "reason": reason
        }), 200

    except Exception as e:
        logger.error(f"Status update failed for request {request_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Status update failed"}), 500


@bp.route("/job-requests/<request_id>/offers", methods=["POST"])
@jwt_required()
@admin_required
def send_offer_to_sellers(request_id):
    """
    POST /api/admin/job-requests/<request_id>/offers
    Body: {
      "seller_ids": ["uuid1", "uuid2"],
      "offered_price": 500,
      "offered_start": "2026-03-01T10:00:00Z",
      "message": "Please review and accept if available"
    }
    """
    data = request.get_json(silent=True) or {}
    seller_ids = data.get("seller_ids", [])
    price = data.get("offered_price")
    start_time = data.get("offered_start")
    message = data.get("message", "")

    if not seller_ids or not isinstance(seller_ids, list):
        return jsonify({"error": "seller_ids must be a non-empty list of UUIDs"}), 400

    try:
        # Validate request
        req = supabase.table("job_requests")\
            .select("id, status, category, buyer_id, title")\
            .eq("id", request_id)\
            .maybe_single()\
            .execute()

        req_data = handle_supabase_response(req)

        if not req_data:
            return jsonify({"error": "Job request not found"}), 404

        if req_data["status"] not in ["pending", "in_review"]:
            return jsonify({"error": f"Request already {req_data['status']}"}), 400

        # Validate sellers
        valid_sellers = []
        for seller_id in seller_ids:
            seller = supabase.table("profiles")\
                .select("id, role, employee_category, is_available")\
                .eq("id", seller_id)\
                .eq("role", "seller")\
                .maybe_single()\
                .execute()

            seller_data = handle_supabase_response(seller)

            if not seller_data or seller_data["employee_category"] != req_data["category"]:
                continue

            if not seller_data["is_available"]:
                continue

            valid_sellers.append(seller_id)

        if not valid_sellers:
            return jsonify({"error": "No valid/available sellers found in this category"}), 400

        # Create offers
        offers = []
        for seller_id in valid_sellers:
            offer = {
                "request_id": request_id,
                "seller_id": seller_id,
                "admin_id": get_jwt_identity(),
                "offered_price": price,
                "offered_start": start_time,
                "message": message,
                "status": "pending",
                "created_at": "now()",
                "updated_at": "now()"
            }
            res = supabase.table("job_offers").insert(offer).execute()
            offer_data = handle_supabase_response(res)
            if offer_data:
                offers.append(offer_data[0])

        # Update request status
        supabase.table("job_requests")\
            .update({"status": "offered", "updated_at": "now()"})\
            .eq("id", request_id)\
            .execute()

        # Notify sellers
        for seller_id in valid_sellers:
            socketio.emit(
                "new_job_offer",
                {
                    "request_id": request_id,
                    "title": req_data["title"],
                    "category": req_data["category"],
                    "offered_price": price,
                    "message": message
                },
                room=seller_id
            )

        return jsonify({
            "message": f"Offer sent to {len(offers)} seller(s)",
            "offers": offers
        }), 201

    except Exception as e:
        logger.error(f"Send offer failed for request {request_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to send offer"}), 500
    
@bp.route("/available-sellers", methods=["GET"])
@jwt_required()
@admin_required
def get_available_sellers():
    category = request.args.get("category")
    if not category:
        return jsonify({"error": "category required"}), 400

    try:
        # Get sellers who have at least one gig in this category + are available
        query = supabase.table("profiles")\
            .select("""
                id, full_name, avatar_url, bio, rating, is_available, employee_category,
                gigs!seller_id (id, title, price, status)
            """)\
            .eq("role", "seller")\
            .eq("is_available", True)\
            .eq("gigs.category", category)\
            .order("rating", desc=True)

        result = query.execute().data or []

        # Format: add gig_count and sample gigs
        formatted = []
        for seller in result:
            gigs = seller.pop("gigs", []) or []
            formatted.append({
                **seller,
                "gig_count": len([g for g in gigs if g["status"] == "published"]),
                "sample_gigs": [g["title"] for g in gigs[:2]]  # first 2 titles
            })

        return jsonify(formatted), 200

    except Exception as e:
        logger.error(f"Available sellers failed: {str(e)}")
        return jsonify({"error": "Failed to load sellers"}), 500

@bp.route("/debug/supabase", methods=["GET"])
def debug_supabase():
    status = supabase.check_connection()
    return jsonify(status), 200

# ────────────────────────────────────────────────
# System Analytics
# ────────────────────────────────────────────────
@bp.route("/analytics", methods=["GET"])
@jwt_required()
@admin_required
def get_analytics():
    try:
        # Reuse your existing get_analytics_summary from supabase_service
        summary = supabase.get_analytics_summary()

        # Add role distribution
        roles = supabase.table("profiles").select("role").execute().data or []
        role_counts = {}
        for r in roles:
            role = r.get("role", "unknown")
            role_counts[role] = role_counts.get(role, 0) + 1

        role_distribution = [
            {"role": role, "count": count}
            for role, count in role_counts.items()
        ]

        return jsonify({
            **summary,
            "role_distribution": role_distribution
        }), 200

    except Exception as e:
        logger.error(f"Analytics failed: {str(e)}", exc_info=True)
        return jsonify({
            "total_users": 0,
            "total_sellers": 0,
            "total_buyers": 0,
            "total_bookings": 0,
            "total_revenue": 0,
            "role_distribution": [],
            "error": "Failed to load analytics"
        }), 200

# ────────────────────────────────────────────────
# System Settings
# ────────────────────────────────────────────────    
@bp.route("/settings", methods=["GET"])
@jwt_required()
@admin_required
def get_settings():
    try:
        # Fetch the single settings row (assuming id=1 or first row)
        res = supabase.table("system_settings")\
            .select("*")\
            .limit(1)\
            .execute()

        if not res.data:
            # Return defaults if no row exists
            defaults = {
                "service_fee_percentage": 10.0,
                "payout_delay_days": 7,
                "min_user_age": 18,
                "require_id_verification": True,
                "auto_ban_after_failed_logins": 5,
                "gig_auto_approval": False,
                "flagged_keywords": "scam,fake,illegal,adult",
                "enable_email_notifications": True,
                "session_timeout_minutes": 30,
                "maintenance_mode": False,
                "max_upload_size_mb": 50,
                "daily_gig_creation_limit": 10,
                "enable_2fa_enforcement": True,
                "last_cache_clear": None,
                "currency": "ZAR",
                "default_language": "en",
                "webhook_urls": {
                    "stripe": "",
                    "email_service": "",
                    "analytics": ""
                },
                "categories": [
                    {"id": "cat1", "name": "Graphic Design", "description": "", "active": True},
                    {"id": "cat2", "name": "Web Development", "description": "", "active": True},
                    # ... more default categories
                ],
                "role_permissions": {
                    "buyer": {"can_post_jobs": True, "can_message": True, "can_book": True},
                    "seller": {"can_create_gigs": True, "can_accept_bookings": True, "can_message": True},
                    "admin": {"can_manage_users": True, "can_approve_gigs": True, "full_access": True}
                }
            }
            return jsonify(defaults), 200

        settings = res.data[0]
        # Parse JSON fields
        settings["categories"] = json.loads(settings.get("categories", "[]"))
        settings["role_permissions"] = json.loads(settings.get("role_permissions", "{}"))
        settings["webhook_urls"] = json.loads(settings.get("webhook_urls", "{}"))

        return jsonify(settings), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/settings", methods=["PATCH"])
@jwt_required()
@admin_required
def update_settings():
    admin_id = get_jwt_identity()
    data = request.get_json()

    if not data:
        return jsonify({"error": "No data provided"}), 400

    errors = []

    # ────────────────────────────────────────────────
    # Validation rules
    # ────────────────────────────────────────────────
    if "service_fee_percentage" in data:
        v = data["service_fee_percentage"]
        if not isinstance(v, (int, float)) or v < 0 or v > 30:
            errors.append("service_fee_percentage must be between 0 and 30")

    if "payout_delay_days" in data:
        v = data["payout_delay_days"]
        if not isinstance(v, int) or v < 0 or v > 30:
            errors.append("payout_delay_days must be 0–30")

    if "min_user_age" in data:
        v = data["min_user_age"]
        if not isinstance(v, int) or v < 13 or v > 21:
            errors.append("min_user_age must be 13–21")

    if "auto_ban_after_failed_logins" in data:
        v = data["auto_ban_after_failed_logins"]
        if not isinstance(v, int) or v < 0 or v > 20:
            errors.append("auto_ban_after_failed_logins must be 0–20")

    if "session_timeout_minutes" in data:
        v = data["session_timeout_minutes"]
        if not isinstance(v, int) or v < 5 or v > 1440:
            errors.append("session_timeout_minutes must be 5–1440")

    if "max_upload_size_mb" in data:
        v = data["max_upload_size_mb"]
        if not isinstance(v, int) or v < 1 or v > 100:
            errors.append("max_upload_size_mb must be 1–100")

    if "daily_gig_creation_limit" in data:
        v = data["daily_gig_creation_limit"]
        if not isinstance(v, int) or v < 1 or v > 100:
            errors.append("daily_gig_creation_limit must be 1–100")

    if "currency" in data:
        v = data["currency"]
        if not isinstance(v, str) or len(v) != 3 or not v.isalpha():
            errors.append("currency must be a valid 3-letter ISO code (e.g. ZAR, USD)")

    if "default_language" in data:
        v = data["default_language"]
        if not isinstance(v, str) or len(v) != 2 or not v.isalpha():
            errors.append("default_language must be a valid 2-letter code (e.g. en, af)")

    if errors:
        return jsonify({"error": "Validation failed", "details": errors}), 400

    # ────────────────────────────────────────────────
    # Proceed with update
    # ────────────────────────────────────────────────
    try:
        allowed_fields = [
            # ... all previous fields ...
            "currency", "default_language", "webhook_urls",
            "categories", "role_permissions"
        ]

        update_payload = {k: v for k, v in data.items() if k in allowed_fields}

        # JSON stringify where needed
        for field in ["categories", "role_permissions", "webhook_urls"]:
            if field in update_payload:
                update_payload[field] = json.dumps(update_payload[field])

        update_payload["updated_at"] = "now()"
        update_payload["updated_by"] = admin_id

        # Update or insert
        res = supabase.table("system_settings")\
            .update(update_payload)\
            .eq("id", 1)\
            .execute()

        if not res.data:
            insert_payload = {"id": 1, **update_payload, "created_at": "now()", "created_by": admin_id}
            supabase.table("system_settings").insert(insert_payload).execute()

        return jsonify({"message": "Settings updated successfully"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ────────────────────────────────────────────────
# Categories
# ────────────────────────────────────────────────   
@bp.route("/categories", methods=["GET"])
@jwt_required()
@admin_required
def list_categories():
    res = supabase.table("system_settings")\
        .select("categories")\
        .limit(1)\
        .execute()
    
    categories = json.loads(res.data[0]["categories"]) if res.data else []
    return jsonify({"categories": categories}), 200


@bp.route("/categories", methods=["POST"])
@jwt_required()
@admin_required
def create_category():
    data = request.get_json()
    name = data.get("name")
    description = data.get("description", "")

    if not name or not name.strip():
        return jsonify({"error": "Name is required"}), 400

    # Get current categories
    res = supabase.table("system_settings").select("categories").limit(1).execute()
    current = json.loads(res.data[0]["categories"]) if res.data else []

    new_cat = {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "description": description.strip(),
        "active": True
    }

    current.append(new_cat)

    supabase.table("system_settings")\
        .update({"categories": json.dumps(current), "updated_at": "now()"})\
        .eq("id", 1)\
        .execute()

    return jsonify({"message": "Category created", "category": new_cat}), 201


@bp.route("/categories/<category_id>", methods=["PATCH"])
@jwt_required()
@admin_required
def update_category(category_id):
    data = request.get_json()
    name = data.get("name")
    active = data.get("active")

    if name is None and active is None:
        return jsonify({"error": "No fields to update"}), 400

    res = supabase.table("system_settings").select("categories").limit(1).execute()
    if not res.data:
        return jsonify({"error": "Settings not found"}), 404

    categories = json.loads(res.data[0]["categories"])
    for cat in categories:
        if cat["id"] == category_id:
            if name is not None:
                cat["name"] = name.strip()
            if active is not None:
                cat["active"] = bool(active)
            break
    else:
        return jsonify({"error": "Category not found"}), 404

    supabase.table("system_settings")\
        .update({"categories": json.dumps(categories), "updated_at": "now()"})\
        .eq("id", 1)\
        .execute()

    return jsonify({"message": "Category updated"}), 200


@bp.route("/categories/<category_id>", methods=["DELETE"])
@jwt_required()
@admin_required
def delete_category(category_id):
    res = supabase.table("system_settings").select("categories").limit(1).execute()
    if not res.data:
        return jsonify({"error": "Settings not found"}), 404

    categories = json.loads(res.data[0]["categories"])
    new_list = [c for c in categories if c["id"] != category_id]

    if len(new_list) == len(categories):
        return jsonify({"error": "Category not found"}), 404

    supabase.table("system_settings")\
        .update({"categories": json.dumps(new_list), "updated_at": "now()"})\
        .eq("id", 1)\
        .execute()

    return jsonify({"message": "Category deleted"}), 200

# =============================================================================
# SUPPORT TICKETS – Admin endpoints
# =============================================================================

@bp.route("/support", methods=["GET"])
@jwt_required()
@admin_required
@limiter.limit("30 per minute")
def list_support_tickets():
    """
    Admin: List support tickets with user info (paginated, filterable)
    Query params: page, per_page, status
    """
    try:
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 20, type=int)
        status_filter = request.args.get("status")

        offset = (page - 1) * per_page

        # Use explicit FK hint to avoid PGRST200
        query = supabase.table("support_tickets")\
            .select("""
                id,
                user_id,
                subject,
                description,
                status,
                created_at,
                escalated_note,
                escalated_at,
                escalated_by,
                resolved_at,
                resolved_by,
                priority,
                category,
                last_activity,
                user:profiles!support_tickets_user_id_fkey (full_name, email)
            """, count="exact")\
            .order("created_at", desc=True)

        if status_filter:
            query = query.eq("status", status_filter)

        res = query.range(offset, offset + per_page - 1).execute()

        tickets = []
        for t in res.data or []:
            user = t.pop("profiles", {}) or {}
            tickets.append({
                **t,
                "user_name": user.get("full_name", "Unknown User"),
                "user_email": user.get("email", "N/A"),
            })

        total = res.count or 0

        return jsonify({
            "tickets": tickets,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if per_page > 0 else 1,
            "has_more": (page * per_page) < total
        }), 200

    except postgrest.exceptions.APIError as e:
        current_app.logger.error(f"PostgREST error listing tickets: {str(e)}")
        return jsonify({
            "tickets": [],
            "total": 0,
            "page": 1,
            "per_page": 20,
            "total_pages": 1,
            "has_more": False,
            "warning": "Partial data - join failed"
        }), 200

    except Exception as e:
        current_app.logger.exception("List support tickets failed")
        return jsonify({
            "tickets": [],
            "total": 0,
            "page": 1,
            "per_page": 20,
            "total_pages": 1,
            "has_more": False,
            "error": "Internal error loading tickets"
        }), 200  # 200 so frontend doesn't crash


@bp.route("/support/<string:ticket_id>/thread", methods=["GET"])
@jwt_required()
@admin_required
@limiter.limit("20 per minute")
def get_ticket_thread(ticket_id: str):
    try:
        # Ticket details with safe join
        ticket_res = supabase.table("support_tickets")\
            .select("""
                id,
                user_id,
                subject,
                description,
                status,
                created_at,
                escalated_note,
                escalated_at,
                escalated_by,
                resolved_at,
                resolved_by,
                priority,
                category,
                last_activity,
                user:profiles!support_tickets_user_id_fkey (full_name, email)
            """)\
            .eq("id", ticket_id)\
            .maybe_single()\
            .execute()

        if not ticket_res.data:
            return jsonify({"error": "Ticket not found"}), 404

        ticket = ticket_res.data
        user = ticket.pop("profiles", {}) or {}
        ticket["user_name"] = user.get("full_name", "Unknown")
        ticket["user_email"] = user.get("email", "N/A")

        # Replies
        replies_res = supabase.table("support_replies")\
            .select("""
                id,
                sender_id,
                message,
                created_at,
                is_admin,
                sender:profiles!sender_id (full_name as sender_name)
            """)\
            .eq("ticket_id", ticket_id)\
            .order("created_at", asc=True)\
            .execute()

        replies = []
        for r in replies_res.data or []:
            sender = r.pop("profiles", {}) or {}
            replies.append({
                **r,
                "sender_name": sender.get("sender_name", "Unknown")
            })

        return jsonify({
            "ticket": ticket,
            "replies": replies
        }), 200

    except Exception as e:
        current_app.logger.exception(f"Ticket thread failed: {ticket_id}")
        return jsonify({"error": "Failed to load thread"}), 500


@bp.route("/support/<string:ticket_id>/reply", methods=["POST"])
@jwt_required()
@admin_required
@limiter.limit("10 per minute")
def add_support_reply(ticket_id: str):
    admin_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()

    if not message:
        return jsonify({"error": "Message required"}), 400

    try:
        ticket_check = supabase.table("support_tickets")\
            .select("id, status")\
            .eq("id", ticket_id)\
            .maybe_single().execute()

        if not ticket_check.data:
            return jsonify({"error": "Ticket not found"}), 404

        if ticket_check.data["status"] in ["closed", "resolved"]:
            return jsonify({"error": "Cannot reply to closed/resolved ticket"}), 400

        reply = {
            "ticket_id": ticket_id,
            "sender_id": admin_id,
            "message": message,
            "is_admin": True,
            "created_at": "now()"
        }

        res = supabase.table("support_replies").insert(reply).execute()

        if not res.data:
            return jsonify({"error": "Failed to send reply"}), 500

        supabase.table("support_tickets")\
            .update({"last_activity": "now()"})\
            .eq("id", ticket_id)\
            .execute()

        # Audit
        supabase.table("audit_logs").insert({
            "user_id": None,
            "action": "admin_reply_ticket",
            "details": {"ticket_id": ticket_id, "admin_id": admin_id}
        }).execute()

        return jsonify({"message": "Reply sent", "reply": res.data[0]}), 201

    except Exception as e:
        current_app.logger.exception(f"Reply failed for ticket {ticket_id}")
        return jsonify({"error": "Failed to send reply"}), 500


@bp.route("/support/<string:ticket_id>/resolve", methods=["PATCH"])
@jwt_required()
@admin_required
@limiter.limit("10 per minute")
def resolve_ticket(ticket_id: str):
    admin_id = get_jwt_identity()

    try:
        res = supabase.table("support_tickets")\
            .update({
                "status": "resolved",
                "resolved_at": "now()",
                "resolved_by": admin_id,
                "last_activity": "now()"
            })\
            .eq("id", ticket_id)\
            .execute()

        if not res.data:
            return jsonify({"error": "Ticket not found"}), 404

        supabase.table("audit_logs").insert({
            "user_id": None,
            "action": "admin_resolve_ticket",
            "details": {"ticket_id": ticket_id, "admin_id": admin_id}
        }).execute()

        return jsonify({"message": "Ticket resolved"}), 200

    except Exception as e:
        current_app.logger.exception(f"Resolve failed: {ticket_id}")
        return jsonify({"error": "Failed to resolve"}), 500


@bp.route("/support/<string:ticket_id>/escalate", methods=["PATCH"])
@jwt_required()
@admin_required
@limiter.limit("5 per minute")
def escalate_ticket(ticket_id: str):
    admin_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    note = data.get("escalated_note", "").strip()

    if not note:
        return jsonify({"error": "Escalation note required"}), 400

    try:
        res = supabase.table("support_tickets")\
            .update({
                "status": "escalated",
                "escalated_note": note,
                "escalated_at": "now()",
                "escalated_by": admin_id,
                "last_activity": "now()"
            })\
            .eq("id", ticket_id)\
            .execute()

        if not res.data:
            return jsonify({"error": "Ticket not found"}), 404

        supabase.table("audit_logs").insert({
            "user_id": None,
            "action": "admin_escalate_ticket",
            "details": {"ticket_id": ticket_id, "admin_id": admin_id, "note": note}
        }).execute()

        return jsonify({"message": "Ticket escalated"}), 200

    except Exception as e:
        current_app.logger.exception(f"Escalate failed: {ticket_id}")
        return jsonify({"error": "Failed to escalate"}), 500
    
@bp.route("/profile/<string:admin_id>", methods=["GET"])
@jwt_required()
@admin_required
def get_admin_profile(admin_id: str):
    current_user_id = get_jwt_identity()
    if current_user_id != admin_id:
        return jsonify({"error": "Unauthorized"}), 403

    try:
        admin = supabase.table("admins")\
            .select("id, email, full_name, admin_level, permissions, last_login, created_at, updated_at")\
            .eq("id", admin_id)\
            .maybe_single().execute()

        if not admin.data:
            return jsonify({"error": "Admin profile not found"}), 404

        return jsonify(admin.data), 200

    except Exception as e:
        logger.error(f"Get admin profile failed: {str(e)}")
        return jsonify({"error": "Failed to load profile"}), 500


@bp.route("/profile/<string:admin_id>", methods=["PATCH"])
@jwt_required()
@admin_required
def update_admin_profile(admin_id: str):
    current_user_id = get_jwt_identity()
    if current_user_id != admin_id:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    updates = {}

    if "full_name" in data:
        updates["full_name"] = data["full_name"].strip()

    if "permissions" in data:
        updates["permissions"] = data["permissions"]

    if "new_password" in data:
        # TODO: validate current password if required
        # For now, just update password via Supabase auth
        try:
            supabase.auth.update_user({"password": data["new_password"]})
        except Exception as e:
            return jsonify({"error": "Failed to update password"}), 400
        updates["updated_at"] = "now()"

    if not updates:
        return jsonify({"error": "No changes provided"}), 400

    try:
        res = supabase.table("admins")\
            .update(updates)\
            .eq("id", admin_id)\
            .execute()

        if not res.data:
            return jsonify({"error": "Profile not found"}), 404

        return jsonify(res.data[0]), 200

    except Exception as e:
        logger.error(f"Update admin profile failed: {str(e)}")
        return jsonify({"error": "Failed to update profile"}), 500
    
@bp.route("/logs", methods=["GET"])
@jwt_required()
@admin_required
def list_audit_logs():
    try:
        page = request.args.get("page", 1, type=int)
        limit = request.args.get("limit", 20, type=int)
        offset = (page - 1) * limit

        query = supabase.table("audit_logs")\
            .select("""
                id,
                user_id,
                action,
                details,
                created_at
            """, count="exact")\
            .order("created_at", desc=True)\
            .range(offset, offset + limit - 1)

        res = query.execute()

        return jsonify({
            "logs": res.data or [],
            "total": res.count or 0,
            "page": page,
            "limit": limit,
            "total_pages": (res.count or 0 + limit - 1) // limit if limit > 0 else 1
        }), 200

    except Exception as e:
        logger.exception("Failed to list audit logs")
        return jsonify({"logs": [], "total": 0, "error": "Failed to load logs"}), 200
    
@bp.route("/log", methods=["POST"])
def create_log():
    data = request.json
    log = {
        "id": str(uuid.uuid4()),
        "user_id": data.get("user_id", "unknown"),
        "action": data.get("action", "unknown"),
        "details": data.get("details", {}),
    }
    broadcast_log(log)
    return jsonify({"status": "ok", "log_id": log["id"]})