# app/routes/support.py
from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.services.supabase_service import supabase
import uuid
import logging
from app.utils.audit import log_action

logger = logging.getLogger(__name__)

bp = Blueprint("support", __name__, url_prefix="/api/support")

# Assuming socketio is imported from your app factory
# If not already global, import it like this:
from app import socketio  # adjust based on your __init__.py structure

# GET /api/support/my-tickets
# Returns ONLY the authenticated user's own tickets
@bp.route("/my-tickets", methods=["GET"])
@jwt_required()
def get_my_tickets():
    user_id = get_jwt_identity()

    try:
        res = supabase.table("support_tickets")\
            .select("""
                id,
                subject,
                description,
                status,
                created_at,
                escalated_note,
                escalated_at,
                resolved_at
            """)\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .execute()

        return jsonify({"tickets": res.data or []}), 200

    except Exception as e:
        logger.exception("Failed to fetch user tickets")
        return jsonify({"tickets": [], "error": str(e)}), 500


# GET /api/support/<ticket_id>/thread
# Only returns the ticket + replies if the ticket belongs to the authenticated user
@bp.route("/<ticket_id>/thread", methods=["GET"])
@jwt_required()
def get_ticket_thread(ticket_id):
    user_id = get_jwt_identity()

    try:
        # Fetch ticket and enforce ownership
        ticket_res = supabase.table("support_tickets")\
            .select("id, user_id, subject, description, status, created_at")\
            .eq("id", ticket_id)\
            .eq("user_id", user_id)\
            .maybe_single()\
            .execute()

        if not ticket_res.data:
            return jsonify({"error": "Ticket not found or you do not have access to it"}), 404

        ticket = ticket_res.data

        # Fetch replies
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

        return jsonify({
            "ticket": ticket,
            "replies": replies_res.data or []
        }), 200

    except Exception as e:
        logger.exception(f"Failed to load ticket thread: {ticket_id}")
        return jsonify({"error": "Failed to load ticket thread"}), 500


# POST /api/support
# User creates a new support ticket
@bp.route("/", methods=["POST"])
@jwt_required()
def create_support_ticket():
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    # Required fields
    subject = (data.get("subject") or "").strip()
    description = (data.get("description") or "").strip()

    if not subject or not description:
        return jsonify({"error": "Subject and description are required"}), 400

    if len(subject) < 5:
        return jsonify({"error": "Subject must be at least 5 characters"}), 400
    if len(description) < 20:
        return jsonify({"error": "Description must be at least 20 characters"}), 400

    # Optional fields
    priority = data.get("priority", "medium")
    if priority not in ["low", "medium", "high"]:
        priority = "medium"

    category = data.get("category")

    try:
        ticket = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "subject": subject,
            "description": description,
            "status": "open",
            "priority": priority,
            "category": category,
            "created_at": "now()",
            "last_activity": "now()"
        }

        res = supabase.table("support_tickets").insert(ticket).execute()

        if not res.data:
            return jsonify({"error": "Failed to create ticket"}), 500

        # Log creation
        log_action(
            actor_id=user_id,
            action="create_support_ticket",
            target_id=None,
            details={"ticket_id": ticket["id"], "subject": subject}
        )

        return jsonify({
            "message": "Ticket created successfully",
            "ticket": res.data[0]
        }), 201

    except Exception as e:
        logger.exception("Failed to create support ticket")
        return jsonify({"error": str(e)}), 500


# PATCH /api/support/<ticket_id>/user-resolved
# User marks their own open ticket as resolved
@bp.route("/<ticket_id>/user-resolved", methods=["PATCH"])
@jwt_required()
def user_mark_resolved(ticket_id):
    user_id = get_jwt_identity()

    try:
        # Verify ownership and status
        ticket = supabase.table("support_tickets")\
            .select("id, user_id, status")\
            .eq("id", ticket_id)\
            .maybe_single().execute().data

        if not ticket:
            return jsonify({"error": "Ticket not found"}), 404

        if ticket["user_id"] != user_id:
            return jsonify({"error": "You do not have permission to resolve this ticket"}), 403

        if ticket["status"] != "open":
            return jsonify({"error": "Only open tickets can be resolved by user"}), 400

        # Update
        res = supabase.table("support_tickets")\
            .update({
                "status": "resolved",
                "resolved_at": "now()",
                "resolved_by": user_id,
                "last_activity": "now()"
            })\
            .eq("id", ticket_id)\
            .execute()

        if not res.data:
            return jsonify({"error": "Failed to resolve ticket"}), 500

        # Log action
        log_action(
            actor_id=user_id,
            action="user_resolve_ticket",
            target_id=None,
            details={"ticket_id": ticket_id}
        )

        return jsonify({"message": "Ticket resolved by user"}), 200

    except Exception as e:
        logger.exception(f"User resolve ticket failed: {ticket_id}")
        return jsonify({"error": "Failed to resolve ticket"}), 500
