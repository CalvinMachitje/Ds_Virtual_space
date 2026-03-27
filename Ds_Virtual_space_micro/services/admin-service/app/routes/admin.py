# Admin routes for managing users, tickets, verifications, gigs, bookings, and payments.
# services/admin-service/app/routes/admin.py
from fastapi import APIRouter, Request, HTTPException, status, Depends
from pydantic import BaseModel, EmailStr
from typing import Dict, Any, Optional, List
import json
import uuid
from datetime import datetime

from app.core.config import settings
from app.dependencies.rate_limiter import limiter
from app.dependencies.auth import get_current_user
from app.services.supabase_service import supabase
from app.utils.audit import log_action
from app.utils.event_bus import publish_event
from app.utils.redis_utils import safe_redis_call

router = APIRouter(prefix="/admin", tags=["admin"])


# =============================================================================
# MODELS
# =============================================================================

class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str
    otp: Optional[str] = None


class TicketUpdateRequest(BaseModel):
    status: Optional[str] = None
    subject: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    assigned_to: Optional[str] = None
    resolution_notes: Optional[str] = None
    reason: Optional[str] = None


class BulkUserActionRequest(BaseModel):
    action: str
    userIds: List[str]


class VerificationActionRequest(BaseModel):
    rejection_reason: Optional[str] = None


class JobOfferRequest(BaseModel):
    seller_ids: List[str]
    offered_price: Optional[float] = None
    offered_start: Optional[str] = None
    message: Optional[str] = None
    notes: Optional[str] = None

# =============================================================================
# HELPERS
# =============================================================================

def parse_pagination(request: Request):
    try:
        page = max(int(request.query_params.get("page", 1)), 1)
        per_page = min(max(int(request.query_params.get("per_page", 20)), 1), 100)
    except (TypeError, ValueError):
        page, per_page = 1, 20
    return {"page": page, "per_page": per_page}


def build_query_with_filters(
    request: Request, 
    table: str, 
    filters: Dict[str, Any], 
    order_by: str = "created_at"
):
    query = supabase.table(table).select("*")
    for key, value in filters.items():
        if value is not None:
            if isinstance(value, bool):
                query = query.eq(key, value)
            elif isinstance(value, str):
                query = query.ilike(key, f"%{value}%")
            else:
                query = query.eq(key, value)

    try:
        page = int(request.query_params.get("page", 1))
        per_page = int(request.query_params.get("per_page", 20))
    except (TypeError, ValueError):
        page, per_page = 1, 20

    from_idx = (page - 1) * per_page
    to_idx = from_idx + per_page - 1

    query = query.range(from_idx, to_idx).order(order_by, desc=True)

    count_query = supabase.table(table).select("count", count="exact")
    for key, value in filters.items():
        if value is not None:
            count_query = count_query.eq(key, value)

    count_result = count_query.execute()
    total = count_result.count if hasattr(count_result, "count") else 0

    return query, {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page if per_page else 1,
        "has_more": (page * per_page) < total
    }

def handle_supabase_response(response):
    if not response:
        raise ValueError("No response from Supabase")
    return response.data or []


def log_admin_action(action: str, target_id: str, details: Dict = None):
    try:
        log_entry = {
            "user_id": get_current_user(),   # This will be replaced by real user_id in dependency
            "action": action,
            "details": details or {},
            "created_at": datetime.utcnow().isoformat()
        }
        supabase.table("audit_logs").insert(log_entry).execute()
    except Exception as e:
        print(f"Audit log failed: {str(e)}")


# =============================================================================
# SECTION: SUPPORT TICKETS
# =============================================================================

@router.get("/tickets")
@limiter.limit("50 per minute")
async def list_tickets(request: Request, current_user: str = Depends(get_current_user)):
    try:
        filters = {
            "status": request.query_params.get("status"),
            "user_id": request.query_params.get("user_id"),
            "priority": request.query_params.get("priority")
        }
        query, page_info = build_query_with_filters(
            request, "support_tickets", filters, order_by="created_at"
        )
        tickets = handle_supabase_response(query.execute()) or []

        return {"tickets": tickets, **page_info}

    except Exception as e:
        print(f"Failed to list tickets: {str(e)}")
        raise HTTPException(500, detail="Failed to fetch tickets")


@router.post("/tickets")
@limiter.limit("20 per minute")
async def create_ticket(
    request: Request,        
    data: Dict[str, Any], 
    current_user: str = Depends(get_current_user)
):
    required = ["user_id", "subject", "description"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        raise HTTPException(400, detail=f"Missing fields: {', '.join(missing)}")

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
        result = handle_supabase_response(resp)

        if not result:
            raise HTTPException(500, detail="Failed to create ticket")

        log_action(current_user, "create_ticket", {
            "user_id": data.get("user_id"), 
            "subject": data.get("subject")
        })

        return result[0] if result else None

    except Exception as e:
        print(f"Ticket creation failed: {str(e)}")
        raise HTTPException(500, detail="Creation failed")


@router.get("/tickets/{ticket_id}")
async def get_ticket(request: Request, ticket_id: str, current_user: str = Depends(get_current_user)):
    try:
        resp = supabase.table("support_tickets").select("*").eq("id", ticket_id).maybe_single().execute()
        ticket = handle_supabase_response(resp)
        if not ticket:
            raise HTTPException(404, detail="Ticket not found")
        return ticket
    except Exception as e:
        print(f"Get ticket failed (ticket_id: {ticket_id}): {str(e)}")
        raise HTTPException(500, detail="Failed to fetch ticket")


@router.patch("/tickets/{ticket_id}")
@limiter.limit("30 per minute")
async def update_ticket(
    request: Request,   
    ticket_id: str, 
    data: Dict[str, Any], 
    current_user: str = Depends(get_current_user)
):
    if not data:
        raise HTTPException(400, detail="No update data provided")

    try:
        resp = supabase.table("support_tickets")\
            .select("status, status_history")\
            .eq("id", ticket_id)\
            .maybe_single().execute()

        current = handle_supabase_response(resp)
        if not current:
            raise HTTPException(404, detail="Ticket not found")

        update_data: Dict[str, Any] = {}
        status_history = current.get("status_history", []) or []

        # Status change with history
        if "status" in data and data.get("status") != current.get("status"):
            reason = data.get("reason", "").strip()
            if not reason:
                raise HTTPException(400, detail="Reason required when changing status")

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

            updated = handle_supabase_response(resp)
            if not updated:
                raise HTTPException(400, detail="Update failed")

            log_action(current_user, "update_ticket", {
                "changed_fields": list(update_data.keys())
            })

            return updated[0] if updated else None

        return {"message": "No changes applied"}

    except Exception as e:
        print(f"Ticket update failed (ticket_id: {ticket_id}): {str(e)}")
        raise HTTPException(500, detail="Update failed")


@router.delete("/tickets/{ticket_id}")
@limiter.limit("5 per minute")
async def delete_ticket(request: Request, ticket_id: str, current_user: str = Depends(get_current_user)):
    try:
        resp = supabase.table("support_tickets").delete().eq("id", ticket_id).execute()
        deleted = handle_supabase_response(resp)
        if not deleted:
            raise HTTPException(404, detail="Ticket not found")

        log_admin_action(
            action="delete_ticket",
            target_id=ticket_id,
            details={"deleted_by": get_current_user()}
        )

        return {"message": "Ticket deleted"}

    except Exception as e:
        print(f"Ticket delete failed (ticket_id: {ticket_id}): {str(e)}")
        raise HTTPException(500, detail="Delete failed")


# =============================================================================
# SECTION: USERS – List all users
# =============================================================================
@router.get("/users")
@limiter.limit("50 per minute")
async def list_users(request: Request, current_user: str = Depends(get_current_user)):
    try:
        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 10))
        search = request.query_params.get("search", "").strip()
        role = request.query_params.get("role", "all")

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

        return {
            "users": res.data or [],
            "total": res.count or 0,
            "page": page,
            "limit": limit,
            "total_pages": ((res.count or 0) + limit - 1) // limit if limit > 0 else 1
        }

    except Exception as e:
        print(f"Admin users list failed: {str(e)}")
        raise HTTPException(500, detail="Failed to fetch users")


# =============================================================================
# SECTION: USERS – Update single user
# =============================================================================
@router.patch("/users/{user_id}")
@limiter.limit("15 per minute")
async def update_user(request: Request, user_id: str, data: Dict[str, Any], current_user: str = Depends(get_current_user)):
    action = data.get("action")
    if action not in ["ban", "unban", "verify", "unverify"]:
        raise HTTPException(400, detail="Invalid action")

    field = "banned" if action in ["ban", "unban"] else "is_verified"
    value = action in ["ban", "verify"]

    try:
        res = supabase.table("profiles")\
            .update({field: value, "updated_at": "now()"})\
            .eq("id", user_id)\
            .execute()

        if not res.data:
            raise HTTPException(404, detail="User not found")

        log_admin_action(
            action=f"admin_{action}",
            target_id=user_id,
            details={
                "admin_id": get_current_user(),
                "field": field,
                "value": value,
                "timestamp": datetime.utcnow().isoformat()
            }
        )

        return {"message": f"User {action}ed successfully"}

    except Exception as e:
        print(f"User update failed: {user_id} - {str(e)}")
        raise HTTPException(500, detail="Update failed")


# =============================================================================
# SECTION: USERS – Bulk update
# =============================================================================
@router.patch("/users/bulk")
@limiter.limit("10 per minute")
async def bulk_user_update(request: Request, data: BulkUserActionRequest, current_user: str = Depends(get_current_user)):
    action = data.action
    user_ids = data.userIds

    if action not in ["ban", "unban", "verify", "unverify"]:
        raise HTTPException(400, detail="Invalid action")

    if not user_ids:
        raise HTTPException(400, detail="No users selected")

    field = "banned" if action in ["ban", "unban"] else "is_verified"
    value = action in ["ban", "verify"]

    try:
        supabase.table("profiles")\
            .update({field: value, "updated_at": "now()"})\
            .in_("id", user_ids)\
            .execute()

        log_admin_action(
            action=f"admin_bulk_{action}",
            target_id=None,
            details={
                "admin_id": get_current_user(),
                "user_ids": user_ids,
                "field": field,
                "value": value
            }
        )

        return {"message": f"Bulk {action} applied to {len(user_ids)} users"}

    except Exception as e:
        print(f"Bulk user update failed: {str(e)}")
        raise HTTPException(500, detail="Bulk action failed")


# =============================================================================
# SECTION: USERS – Delete/Suspend user
# =============================================================================
@router.delete("/users/{user_id}")
@limiter.limit("5 per minute")
async def delete_user(request: Request, user_id: str, current_user: str = Depends(get_current_user)):
    if current_user == user_id:
        raise HTTPException(403, detail="Cannot delete your own account")

    try:
        supabase.table("profiles")\
            .update({"banned": True, "updated_at": "now()"})\
            .eq("id", user_id)\
            .execute()

        log_admin_action(
            action="admin_delete_user",
            target_id=user_id,
            details={"admin_id": current_user}
        )

        return {"message": "User suspended/deleted"}

    except Exception as e:
        print(f"User delete failed: {user_id} - {str(e)}")
        raise HTTPException(500, detail="Delete failed")


# =============================================================================
# SECTION: VERIFICATIONS – List pending verifications
# =============================================================================
@router.get("/verifications/pending")
@limiter.limit("30 per minute")
async def list_pending_verifications(request: Request, current_user: str = Depends(get_current_user)):
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

        return {"verifications": res.data or []}

    except Exception as e:
        print(f"Pending verifications fetch failed: {str(e)}")
        raise HTTPException(500, detail="Failed to load pending verifications")


# =============================================================================
# SECTION: VERIFICATIONS – Get single verification details
# =============================================================================
@router.get("/verifications/{verification_id}")
@limiter.limit("20 per minute")
async def get_verification( request: Request, verification_id: str, current_user: str = Depends(get_current_user)):
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
            raise HTTPException(404, detail="Verification not found")

        return res.data

    except Exception as e:
        print(f"Verification fetch failed: {verification_id} - {str(e)}")
        raise HTTPException(500, detail="Failed to load verification")


# =============================================================================
# SECTION: VERIFICATIONS – Approve verification
# =============================================================================
@router.patch("/verifications/{verification_id}/approve")
@limiter.limit("10 per minute")
async def approve_verification( request: Request, verification_id: str, current_user: str = Depends(get_current_user)):
    try:
        ver = supabase.table("verifications")\
            .select("seller_id")\
            .eq("id", verification_id)\
            .maybe_single().execute()

        if not ver.data:
            raise HTTPException(404, detail="Verification not found")

        seller_id = ver.data["seller_id"]

        supabase.table("verifications")\
            .update({
                "status": "approved",
                "reviewed_by": current_user,
                "reviewed_at": "now()",
                "updated_at": "now()"
            })\
            .eq("id", verification_id)\
            .execute()

        supabase.table("profiles")\
            .update({
                "is_verified": True,
                "updated_at": "now()"
            })\
            .eq("id", seller_id)\
            .execute()

        log_admin_action(
            action="admin_verify_approved",
            target_id=verification_id,
            details={"admin_id": current_user}
        )

        return {"message": "Seller verified successfully"}

    except Exception as e:
        print(f"Approve verification failed: {verification_id} - {str(e)}")
        raise HTTPException(500, detail="Approval failed")


# =============================================================================
# SECTION: VERIFICATIONS – Reject verification
# =============================================================================
@router.patch("/verifications/{verification_id}/reject")
@limiter.limit("10 per minute")
async def reject_verification(request: Request, verification_id: str, data: VerificationActionRequest, current_user: str = Depends(get_current_user)):
    try:
        supabase.table("verifications").update({
            "status": "rejected",
            "rejection_reason": data.rejection_reason,
            "reviewed_by": current_user,
            "reviewed_at": "now()",
            "updated_at": "now()"
        }).eq("id", verification_id).execute()

        log_admin_action(
            action="admin_verify_rejected",
            target_id=verification_id,
            details={"admin_id": current_user, "reason": data.rejection_reason}
        )

        return {"message": "Verification rejected"}

    except Exception as e:
        print(f"Reject verification failed: {verification_id} - {str(e)}")
        raise HTTPException(500, detail="Rejection failed")


print("Admin routes (first half) converted to FastAPI successfully")

# =============================================================================
# MANAGE GIGS
# =============================================================================
@router.get("/gigs")
@limiter.limit("50 per minute")
async def list_gigs(request: Request, current_user: str = Depends(get_current_user)):
    try:
        query = supabase.table("gigs")\
            .select("*, profiles!seller_id (full_name, email)")\
            .order("created_at", desc=True)

        # Optional filters
        status_filter = request.query_params.get("status")
        if status_filter:
            query = query.eq("status", status_filter)

        gigs = handle_supabase_response(query.execute())

        return {"gigs": gigs}

    except Exception as e:
        print(f"List gigs failed: {str(e)}")
        raise HTTPException(500, detail="Failed to load gigs")


@router.patch("/gigs/{gig_id}/status")
@limiter.limit("20 per minute")
async def update_gig_status(request: Request, gig_id: str, data: Dict[str, Any], current_user: str = Depends(get_current_user)):    
    new_status = data.get("status")

    if new_status not in ["active", "rejected"]:
        raise HTTPException(400, detail="Invalid status")

    try:
        updated = supabase.table("gigs")\
            .update({"status": new_status, "updated_at": "now()"})\
            .eq("id", gig_id)\
            .execute()

        if not updated.data:
            raise HTTPException(404, detail="Gig not found")

        log_action(current_user, "update_gig_status", {
            "gig_id": gig_id,
            "new_status": new_status
        })

        return {"message": f"Gig status updated to {new_status}"}

    except Exception as e:
        print(f"Update gig status failed: {str(e)}")
        raise HTTPException(500, detail="Failed to update gig")


# =============================================================================
# BOOKINGS – Admin endpoints
# =============================================================================
@router.get("/bookings")
@limiter.limit("30 per minute")
async def list_bookings(request: Request, current_user: str = Depends(get_current_user)):
    try:
        filters = {
            "status": request.query_params.get("status"),
            "buyer_id": request.query_params.get("buyer_id"),
            "seller_id": request.query_params.get("seller_id")
        }

        query, page_info = build_query_with_filters(
            request, "bookings", filters, order_by="created_at"
        )

        bookings = handle_supabase_response(query.execute()) or []

        return {"bookings": bookings, **page_info}

    except Exception as e:
        print(f"List bookings failed: {str(e)}")
        # Return safe empty response so frontend doesn't crash
        return {
            "bookings": [],
            "total": 0,
            "page": 1,
            "per_page": 20,
            "total_pages": 1,
            "has_more": False
        }


@router.patch("/bookings/{booking_id}")
@limiter.limit("15 per minute")
async def update_booking(request: Request, booking_id: str, data: Dict[str, Any], current_user: str = Depends(get_current_user)):
    allowed = ["status", "price", "service", "cancel_reason", "requirements", "notes"]
    update_data = {k: v for k, v in data.items() if k in allowed and v is not None}

    if not update_data:
        raise HTTPException(400, detail="No valid fields provided for update")

    try:
        resp = supabase.table("bookings")\
            .update({**update_data, "updated_at": "now()"})\
            .eq("id", booking_id)\
            .execute()

        updated = handle_supabase_response(resp)
        if not updated:
            raise HTTPException(404, detail="Booking not found or update failed")

        log_admin_action(
            action="update_booking",
            target_id=booking_id,
            details={
                "changed_fields": list(update_data.keys()),
                "new_values": update_data
            }
        )

        return updated[0] if updated else None

    except Exception as e:
        print(f"Booking update failed (booking_id: {booking_id}): {str(e)}")
        raise HTTPException(500, detail="Update failed")


@router.patch("/bookings/{booking_id}/status")
async def update_booking_status(request: Request, booking_id: str, data: Dict[str, Any], current_user: str = Depends(get_current_user)):
    new_status = data.get("status")

    if not new_status or new_status not in ["pending", "active", "completed", "cancelled"]:
        raise HTTPException(400, detail="Invalid status")

    try:
        updated = supabase.table("bookings")\
            .update({"status": new_status, "updated_at": "now()"})\
            .eq("id", booking_id)\
            .execute()

        if not updated.data:
            raise HTTPException(404, detail="Booking not found")

        log_action(current_user, "update_booking_status", {
            "booking_id": booking_id,
            "new_status": new_status
        })

        return {
            "message": f"Booking status updated to {new_status}",
            "booking": updated.data[0]
        }

    except Exception as e:
        print(f"Update booking status failed: {str(e)}")
        raise HTTPException(500, detail="Update failed")


# =============================================================================
# PAYMENTS
# =============================================================================
@router.get("/payments")
@limiter.limit("20 per minute")
async def list_payments(request: Request, current_user: str = Depends(get_current_user)):
    try:
        filters = {"status": request.query_params.get("status")}
        query, page_info = build_query_with_filters(
            request, "payments", filters, order_by="created_at"
        )
        payments = handle_supabase_response(query.execute()) or []

        return {"payments": payments, **page_info}

    except Exception as e:
        print(f"List payments failed: {str(e)}")
        raise HTTPException(500, detail="Failed to fetch payments")


@router.patch("/payments/{payment_id}/refund")
@limiter.limit("5 per minute")
async def refund_payment(request: Request, payment_id: str, current_user: str = Depends(get_current_user)):
    try:
        resp = supabase.table("payments")\
            .update({"status": "refunded", "updated_at": "now()"})\
            .eq("id", payment_id)\
            .execute()

        updated = handle_supabase_response(resp)
        if not updated:
            raise HTTPException(404, detail="Payment not found")

        log_admin_action(
            action="refund_payment",
            target_id=payment_id,
            details={"refunded_by": current_user}
        )

        return updated[0] if updated else None

    except Exception as e:
        print(f"Refund failed (payment_id: {payment_id}): {str(e)}")
        raise HTTPException(500, detail="Refund failed")


# =============================================================================
# ANALYTICS / DASHBOARD
# =============================================================================
@router.get("/dashboard")
async def admin_dashboard(request: Request, current_user: str = Depends(get_current_user)):
    try:
        total_users = supabase.table("profiles").select("count", count="exact").execute().count or 0
        pending_verifs = supabase.table("verifications").select("count", count="exact").eq("status", "pending").execute().count or 0
        open_tickets = supabase.table("support_tickets").select("count", count="exact").eq("status", "open").execute().count or 0
        active_gigs = supabase.table("gigs").select("count", count="exact").eq("status", "published").execute().count or 0

        return {
            "total_users": total_users,
            "pending_verifications": pending_verifs,
            "open_tickets": open_tickets,
            "active_gigs": active_gigs
        }

    except Exception as e:
        print(f"Dashboard failed: {str(e)}")
        raise HTTPException(500, detail="Failed to load dashboard")


# =============================================================================
# JOB REQUESTS – Admin endpoints
# =============================================================================
@router.get("/job-requests")
async def list_job_requests(request: Request, current_user: str = Depends(get_current_user)):
    status_filter = request.query_params.get("status", "pending")
    try:
        reqs = supabase.table("job_requests")\
            .select("*, profiles!buyer_id (full_name, email, phone)")\
            .eq("status", status_filter)\
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

        return {"job_requests": formatted}

    except Exception as e:
        print(f"List job requests failed: {str(e)}")
        raise HTTPException(500, detail="Failed to load requests")


@router.get("/job-requests/{request_id}")
async def get_job_request(request_id: str, current_user: str = Depends(get_current_user)):
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
            raise HTTPException(404, detail="Job request not found")

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

        return response

    except Exception as e:
        print(f"Failed to get job request {request_id}: {str(e)}")
        raise HTTPException(500, detail="Failed to fetch job request")


@router.patch("/job-requests/{request_id}/assign")
async def assign_seller_to_job_request(request_id: str, data: JobOfferRequest, current_user: str = Depends(get_current_user)):
    seller_ids = data.seller_ids
    notes = data.notes or ""

    if not seller_ids or not isinstance(seller_ids, list):
        raise HTTPException(400, detail="seller_ids must be a non-empty list")

    try:
        req = supabase.table("job_requests")\
            .select("id, status, category, buyer_id, title")\
            .eq("id", request_id)\
            .maybe_single()\
            .execute()

        req_data = handle_supabase_response(req)
        if not req_data:
            raise HTTPException(404, detail="Job request not found")

        if req_data["status"] != "pending":
            raise HTTPException(400, detail=f"Request is already {req_data['status']}")

        assigned = []
        for seller_id in seller_ids:
            seller = supabase.table("profiles")\
                .select("id, role, employee_category, is_available")\
                .eq("id", seller_id)\
                .eq("role", "seller")\
                .maybe_single()\
                .execute()

            seller_data = handle_supabase_response(seller)
            if not seller_data:
                continue

            if seller_data.get("employee_category") != req_data["category"]:
                continue
            if not seller_data.get("is_available"):
                continue

            update = supabase.table("job_requests")\
                .update({
                    "assigned_seller_id": seller_id,
                    "status": "assigned",
                    "updated_at": "now()"
                })\
                .eq("id", request_id)\
                .execute()

            if update.data:
                assigned.append(seller_id)

            # TODO: Replace socketio with your event_bus or WebSocket manager
            publish_event("job.events", {
                "event": "new_job_assignment",
                "request_id": request_id,
                "buyer_id": req_data["buyer_id"],
                "category": req_data["category"],
                "title": req_data.get("title"),
                "notes": notes
            })

        if not assigned:
            raise HTTPException(400, detail="No valid sellers could be assigned")

        return {
            "message": f"Assigned {len(assigned)} seller(s)",
            "assigned_seller_ids": assigned
        }

    except Exception as e:
        print(f"Assign failed for {request_id}: {str(e)}")
        raise HTTPException(500, detail="Failed to assign seller(s)")


@router.patch("/job-requests/{request_id}/status")
async def update_job_request_status( request: Request, request_id: str, data: Dict[str, Any], current_user: str = Depends(get_current_user)):
    new_status = data.get("status")
    reason = data.get("reason", "").strip()

    valid_statuses = ["cancelled", "rejected"]
    if not new_status or new_status not in valid_statuses:
        raise HTTPException(400, detail=f"Valid statuses: {', '.join(valid_statuses)}")

    try:
        updated = supabase.table("job_requests")\
            .update({
                "status": new_status,
                "updated_at": "now()"
            })\
            .eq("id", request_id)\
            .execute()

        if not updated.data:
            raise HTTPException(404, detail="Job request not found")

        log_action(current_user, "update_job_request_status", {
            "request_id": request_id,
            "new_status": new_status,
            "reason": reason
        })

        return {
            "message": f"Request marked as {new_status}",
            "request_id": request_id,
            "reason": reason
        }

    except Exception as e:
        print(f"Status update failed for request {request_id}: {str(e)}")
        raise HTTPException(500, detail="Status update failed")


@router.post("/job-requests/{request_id}/offers")
async def send_offer_to_sellers(request: Request, request_id: str, data: JobOfferRequest, current_user: str = Depends(get_current_user)):
    seller_ids = data.seller_ids
    price = data.offered_price
    start_time = data.offered_start
    message = data.message or ""

    if not seller_ids or not isinstance(seller_ids, list):
        raise HTTPException(400, detail="seller_ids must be a non-empty list of UUIDs")

    try:
        req = supabase.table("job_requests")\
            .select("id, status, category, buyer_id, title")\
            .eq("id", request_id)\
            .maybe_single()\
            .execute()

        req_data = handle_supabase_response(req)
        if not req_data:
            raise HTTPException(404, detail="Job request not found")

        if req_data["status"] not in ["pending", "in_review"]:
            raise HTTPException(400, detail=f"Request already {req_data['status']}")

        valid_sellers = []
        for seller_id in seller_ids:
            seller = supabase.table("profiles")\
                .select("id, role, employee_category, is_available")\
                .eq("id", seller_id)\
                .eq("role", "seller")\
                .maybe_single()\
                .execute()

            seller_data = handle_supabase_response(seller)
            if not seller_data or seller_data.get("employee_category") != req_data["category"]:
                continue
            if not seller_data.get("is_available"):
                continue
            valid_sellers.append(seller_id)

        if not valid_sellers:
            raise HTTPException(400, detail="No valid/available sellers found in this category")

        offers = []
        for seller_id in valid_sellers:
            offer = {
                "request_id": request_id,
                "seller_id": seller_id,
                "admin_id": current_user,
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

        # Notify sellers via event bus (replace socketio.emit)
        for seller_id in valid_sellers:
            publish_event("job.events", {
                "event": "new_job_offer",
                "request_id": request_id,
                "title": req_data.get("title"),
                "category": req_data["category"],
                "offered_price": price,
                "message": message
            })

        return {
            "message": f"Offer sent to {len(offers)} seller(s)",
            "offers": offers
        }

    except Exception as e:
        print(f"Send offer failed for request {request_id}: {str(e)}")
        raise HTTPException(500, detail="Failed to send offer")


print("Gigs, Bookings, Payments, Dashboard & Job Requests converted to FastAPI")
    
# =============================================================================
# AVAILABLE SELLERS
# =============================================================================
@router.get("/available-sellers")
@limiter.limit("30 per minute")
async def get_available_sellers(request: Request, current_user: str = Depends(get_current_user)):
    category = request.query_params.get("category")
    if not category:
        raise HTTPException(400, detail="category required")

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
                "gig_count": len([g for g in gigs if g.get("status") == "published"]),
                "sample_gigs": [g.get("title") for g in gigs[:2]]  # first 2 titles
            })

        return {"sellers": formatted}

    except Exception as e:
        print(f"Available sellers failed: {str(e)}")
        raise HTTPException(500, detail="Failed to load sellers")


# =============================================================================
# DEBUG
# =============================================================================
@router.get("/debug/supabase")
async def debug_supabase(request: Request, current_user: str = Depends(get_current_user)):
    status = supabase.check_connection()
    return status


# =============================================================================
# SYSTEM ANALYTICS
# =============================================================================
@router.get("/analytics")
@limiter.limit("20 per minute")
async def get_analytics(request: Request, current_user: str = Depends(get_current_user)):
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

        return {
            **summary,
            "role_distribution": role_distribution
        }

    except Exception as e:
        print(f"Analytics failed: {str(e)}")
        return {
            "total_users": 0,
            "total_sellers": 0,
            "total_buyers": 0,
            "total_bookings": 0,
            "total_revenue": 0,
            "role_distribution": [],
            "error": "Failed to load analytics"
        }


# =============================================================================
# SYSTEM SETTINGS
# =============================================================================
@router.get("/settings")
@limiter.limit("30 per minute")
async def get_settings(request: Request, current_user: str = Depends(get_current_user)):
    try:
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
                ],
                "role_permissions": {
                    "buyer": {"can_post_jobs": True, "can_message": True, "can_book": True},
                    "seller": {"can_create_gigs": True, "can_accept_bookings": True, "can_message": True},
                    "admin": {"can_manage_users": True, "can_approve_gigs": True, "full_access": True}
                }
            }
            return defaults

        settings = res.data[0]
        # Parse JSON fields
        settings["categories"] = json.loads(settings.get("categories", "[]"))
        settings["role_permissions"] = json.loads(settings.get("role_permissions", "{}"))
        settings["webhook_urls"] = json.loads(settings.get("webhook_urls", "{}"))

        return settings

    except Exception as e:
        print(f"Get settings failed: {str(e)}")
        raise HTTPException(500, detail=str(e))


@router.patch("/settings")
@limiter.limit("10 per minute")
async def update_settings(request: Request, data: Dict[str, Any], current_user: str = Depends(get_current_user)):
    if not data:
        raise HTTPException(400, detail="No data provided")

    errors = []

    # Validation rules
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
        raise HTTPException(400, detail={"error": "Validation failed", "details": errors})

    try:
        allowed_fields = [
            "service_fee_percentage", "payout_delay_days", "min_user_age",
            "require_id_verification", "auto_ban_after_failed_logins",
            "gig_auto_approval", "flagged_keywords", "enable_email_notifications",
            "session_timeout_minutes", "maintenance_mode", "max_upload_size_mb",
            "daily_gig_creation_limit", "enable_2fa_enforcement", "currency",
            "default_language", "webhook_urls", "categories", "role_permissions"
        ]

        update_payload = {k: v for k, v in data.items() if k in allowed_fields}

        # JSON stringify where needed
        for field in ["categories", "role_permissions", "webhook_urls"]:
            if field in update_payload:
                update_payload[field] = json.dumps(update_payload[field])

        update_payload["updated_at"] = "now()"
        update_payload["updated_by"] = current_user

        # Update or insert
        res = supabase.table("system_settings")\
            .update(update_payload)\
            .eq("id", 1)\
            .execute()

        if not res.data:
            insert_payload = {"id": 1, **update_payload, "created_at": "now()", "created_by": current_user}
            supabase.table("system_settings").insert(insert_payload).execute()

        return {"message": "Settings updated successfully"}

    except Exception as e:
        print(f"Update settings failed: {str(e)}")
        raise HTTPException(500, detail=str(e))


# =============================================================================
# CATEGORIES
# =============================================================================
@router.get("/categories")
async def list_categories(request: Request, current_user: str = Depends(get_current_user)):
    try:
        res = supabase.table("system_settings")\
            .select("categories")\
            .limit(1)\
            .execute()
        
        categories = json.loads(res.data[0]["categories"]) if res.data else []
        return {"categories": categories}
    except Exception as e:
        print(f"List categories failed: {str(e)}")
        raise HTTPException(500, detail="Failed to load categories")


@router.post("/categories")
async def create_category(request: Request, data: Dict[str, Any], current_user: str = Depends(get_current_user)):
    name = data.get("name")
    description = data.get("description", "")

    if not name or not name.strip():
        raise HTTPException(400, detail="Name is required")

    try:
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

        return {"message": "Category created", "category": new_cat}

    except Exception as e:
        print(f"Create category failed: {str(e)}")
        raise HTTPException(500, detail="Failed to create category")


@router.patch("/categories/{category_id}")
async def update_category(request: Request, category_id: str, data: Dict[str, Any], current_user: str = Depends(get_current_user)):
    name = data.get("name")
    active = data.get("active")

    if name is None and active is None:
        raise HTTPException(400, detail="No fields to update")

    try:
        res = supabase.table("system_settings").select("categories").limit(1).execute()
        if not res.data:
            raise HTTPException(404, detail="Settings not found")

        categories = json.loads(res.data[0]["categories"])
        for cat in categories:
            if cat["id"] == category_id:
                if name is not None:
                    cat["name"] = name.strip()
                if active is not None:
                    cat["active"] = bool(active)
                break
        else:
            raise HTTPException(404, detail="Category not found")

        supabase.table("system_settings")\
            .update({"categories": json.dumps(categories), "updated_at": "now()"})\
            .eq("id", 1)\
            .execute()

        return {"message": "Category updated"}

    except Exception as e:
        print(f"Update category failed: {str(e)}")
        raise HTTPException(500, detail="Failed to update category")


@router.delete("/categories/{category_id}")
async def delete_category(request: Request, category_id: str, current_user: str = Depends(get_current_user)):
    try:
        res = supabase.table("system_settings").select("categories").limit(1).execute()
        if not res.data:
            raise HTTPException(404, detail="Settings not found")

        categories = json.loads(res.data[0]["categories"])
        new_list = [c for c in categories if c["id"] != category_id]

        if len(new_list) == len(categories):
            raise HTTPException(404, detail="Category not found")

        supabase.table("system_settings")\
            .update({"categories": json.dumps(new_list), "updated_at": "now()"})\
            .eq("id", 1)\
            .execute()

        return {"message": "Category deleted"}

    except Exception as e:
        print(f"Delete category failed: {str(e)}")
        raise HTTPException(500, detail="Failed to delete category")


# =============================================================================
# SUPPORT TICKETS – Advanced endpoints
# =============================================================================
@router.get("/support")
@limiter.limit("30 per minute")
async def list_support_tickets(request: Request, current_user: str = Depends(get_current_user)):
    try:
        page = int(request.query_params.get("page", 1))
        per_page = int(request.query_params.get("per_page", 20))
        status_filter = request.query_params.get("status")

        offset = (page - 1) * per_page

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

        return {
            "tickets": tickets,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if per_page > 0 else 1,
            "has_more": (page * per_page) < total
        }

    except Exception as e:
        print(f"List support tickets failed: {str(e)}")
        return {
            "tickets": [],
            "total": 0,
            "page": 1,
            "per_page": 20,
            "total_pages": 1,
            "has_more": False,
            "error": "Internal error loading tickets"
        }


@router.get("/support/{ticket_id}/thread")
@limiter.limit("20 per minute")
async def get_ticket_thread(request: Request, ticket_id: str, current_user: str = Depends(get_current_user)):
    try:
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
            raise HTTPException(404, detail="Ticket not found")

        ticket = ticket_res.data
        user = ticket.pop("profiles", {}) or {}
        ticket["user_name"] = user.get("full_name", "Unknown")
        ticket["user_email"] = user.get("email", "N/A")

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

        return {
            "ticket": ticket,
            "replies": replies
        }

    except Exception as e:
        print(f"Ticket thread failed: {ticket_id} - {str(e)}")
        raise HTTPException(500, detail="Failed to load thread")


@router.post("/support/{ticket_id}/reply")
@limiter.limit("10 per minute")
async def add_support_reply(request: Request, ticket_id: str, data: Dict[str, Any], current_user: str = Depends(get_current_user)):
    message = data.get("message", "").strip()
    if not message:
        raise HTTPException(400, detail="Message required")

    try:
        ticket_check = supabase.table("support_tickets")\
            .select("id, status")\
            .eq("id", ticket_id)\
            .maybe_single().execute()

        if not ticket_check.data:
            raise HTTPException(404, detail="Ticket not found")

        if ticket_check.data["status"] in ["closed", "resolved"]:
            raise HTTPException(400, detail="Cannot reply to closed/resolved ticket")

        reply = {
            "ticket_id": ticket_id,
            "sender_id": current_user,
            "message": message,
            "is_admin": True,
            "created_at": "now()"
        }

        res = supabase.table("support_replies").insert(reply).execute()

        if not res.data:
            raise HTTPException(500, detail="Failed to send reply")

        supabase.table("support_tickets")\
            .update({"last_activity": "now()"})\
            .eq("id", ticket_id)\
            .execute()

        log_admin_action(
            action="admin_reply_ticket",
            target_id=ticket_id,
            details={"ticket_id": ticket_id, "admin_id": current_user}
        )

        return {"message": "Reply sent", "reply": res.data[0]}

    except Exception as e:
        print(f"Reply failed for ticket {ticket_id}: {str(e)}")
        raise HTTPException(500, detail="Failed to send reply")


@router.patch("/support/{ticket_id}/resolve")
@limiter.limit("10 per minute")
async def resolve_ticket(request: Request, ticket_id: str, current_user: str = Depends(get_current_user)):
    try:
        res = supabase.table("support_tickets")\
            .update({
                "status": "resolved",
                "resolved_at": "now()",
                "resolved_by": current_user,
                "last_activity": "now()"
            })\
            .eq("id", ticket_id)\
            .execute()

        if not res.data:
            raise HTTPException(404, detail="Ticket not found")

        log_admin_action(
            action="admin_resolve_ticket",
            target_id=ticket_id,
            details={"ticket_id": ticket_id, "admin_id": current_user}
        )

        return {"message": "Ticket resolved"}

    except Exception as e:
        print(f"Resolve failed: {ticket_id} - {str(e)}")
        raise HTTPException(500, detail="Failed to resolve")


@router.patch("/support/{ticket_id}/escalate")
@limiter.limit("5 per minute")
async def escalate_ticket(request: Request, ticket_id: str, data: Dict[str, Any], current_user: str = Depends(get_current_user)):
    note = data.get("escalated_note", "").strip()
    if not note:
        raise HTTPException(400, detail="Escalation note required")

    try:
        res = supabase.table("support_tickets")\
            .update({
                "status": "escalated",
                "escalated_note": note,
                "escalated_at": "now()",
                "escalated_by": current_user,
                "last_activity": "now()"
            })\
            .eq("id", ticket_id)\
            .execute()

        if not res.data:
            raise HTTPException(404, detail="Ticket not found")

        log_admin_action(
            action="admin_escalate_ticket",
            target_id=ticket_id,
            details={"ticket_id": ticket_id, "admin_id": current_user, "note": note}
        )

        return {"message": "Ticket escalated"}

    except Exception as e:
        print(f"Escalate failed: {ticket_id} - {str(e)}")
        raise HTTPException(500, detail="Failed to escalate")


# =============================================================================
# ADMIN PROFILE
# =============================================================================
@router.get("/profile/{admin_id}")
async def get_admin_profile(request: Request, admin_id: str, current_user: str = Depends(get_current_user)):
    if current_user != admin_id:
        raise HTTPException(403, detail="Unauthorized")

    try:
        admin = supabase.table("admins")\
            .select("id, email, full_name, admin_level, permissions, last_login, created_at, updated_at")\
            .eq("id", admin_id)\
            .maybe_single().execute()

        if not admin.data:
            raise HTTPException(404, detail="Admin profile not found")

        return admin.data

    except Exception as e:
        print(f"Get admin profile failed: {str(e)}")
        raise HTTPException(500, detail="Failed to load profile")


@router.patch("/profile/{admin_id}")
async def update_admin_profile(request: Request, admin_id: str, data: Dict[str, Any], current_user: str = Depends(get_current_user)):
    if current_user != admin_id:
        raise HTTPException(403, detail="Unauthorized")

    updates = {}

    if "full_name" in data:
        updates["full_name"] = data["full_name"].strip()

    if "permissions" in data:
        updates["permissions"] = data["permissions"]

    if "new_password" in data:
        try:
            supabase.auth.update_user({"password": data["new_password"]})
        except Exception:
            raise HTTPException(400, detail="Failed to update password")
        updates["updated_at"] = "now()"

    if not updates:
        raise HTTPException(400, detail="No changes provided")

    try:
        res = supabase.table("admins")\
            .update(updates)\
            .eq("id", admin_id)\
            .execute()

        if not res.data:
            raise HTTPException(404, detail="Profile not found")

        return res.data[0]

    except Exception as e:
        print(f"Update admin profile failed: {str(e)}")
        raise HTTPException(500, detail="Failed to update profile")


# =============================================================================
# LOGS
# =============================================================================
@router.get("/logs")
@limiter.limit("30 per minute")
async def list_audit_logs(request: Request, current_user: str = Depends(get_current_user)):
    try:
        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 20))
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

        return {
            "logs": res.data or [],
            "total": res.count or 0,
            "page": page,
            "limit": limit,
            "total_pages": (res.count or 0 + limit - 1) // limit if limit > 0 else 1
        }

    except Exception as e:
        print(f"Failed to list audit logs: {str(e)}")
        return {"logs": [], "total": 0, "error": "Failed to load logs"}


@router.post("/log")
async def create_log(request: Request, data: Dict[str, Any]):
    log = {
        "id": str(uuid.uuid4()),
        "user_id": data.get("user_id", "unknown"),
        "action": data.get("action", "unknown"),
        "details": data.get("details", {}),
    }
    # Replace broadcast_log with your event_bus if needed
    publish_event("audit.logs", log)
    return {"status": "ok", "log_id": log["id"]}


print("Final part of admin routes converted to FastAPI successfully")