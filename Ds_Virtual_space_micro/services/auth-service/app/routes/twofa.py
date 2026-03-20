# services/auth-service/app/routes/twofa.py
from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies.auth import get_current_user
from app.services.supabase_service import supabase
from app.utils.audit import log_action
from app.utils.event_bus import publish_event


router = APIRouter(prefix="/2fa", tags=["2fa"])


@router.post("/setup")
async def setup_2fa(current_user: str = Depends(get_current_user)):
    try:
        factor = supabase.auth.mfa.enroll({
            "factor_type": "totp",
            "issuer": "D's Virtual Space",
            "user_id": current_user
        })

        if not factor:
            raise HTTPException(500, detail="2FA enrollment failed")

        publish_event("auth.events", {"event": "2fa_setup_started", "user_id": current_user})

        return {
            "success": True,
            "qr_code": factor.totp.qr_code,
            "secret": factor.totp.secret,
            "factor_id": factor.id
        }
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/verify")
async def verify_2fa(code: str, factor_id: str, current_user: str = Depends(get_current_user)):
    try:
        verified = supabase.auth.mfa.verify({"factor_id": factor_id, "code": code})
        if not verified:
            raise HTTPException(401, detail="Invalid 2FA code")

        supabase.table("profiles").update({"two_factor_enabled": True, "updated_at": "now()"}).eq("id", current_user).execute()

        log_action(current_user, "2fa_enabled")
        publish_event("auth.events", {"event": "2fa_enabled", "user_id": current_user})

        return {"success": True, "message": "2FA enabled"}
    except Exception as e:
        raise HTTPException(400, detail=str(e))


@router.post("/disable")
async def disable_2fa(code: str, current_user: str = Depends(get_current_user)):
    try:
        factors = supabase.auth.mfa.list_user_factors()
        totp = next((f for f in factors if f.factor_type == "totp"), None)
        if not totp:
            raise HTTPException(400, detail="No TOTP factor found")

        verified = supabase.auth.mfa.verify({"factor_id": totp.id, "code": code})
        if not verified:
            raise HTTPException(401, detail="Invalid code")

        supabase.auth.mfa.unenroll(totp.id)
        supabase.table("profiles").update({"two_factor_enabled": False, "updated_at": "now()"}).eq("id", current_user).execute()

        log_action(current_user, "2fa_disabled")
        publish_event("auth.events", {"event": "2fa_disabled", "user_id": current_user})

        return {"success": True, "message": "2FA disabled"}
    except Exception as e:
        raise HTTPException(400, detail=str(e))


def verify_2fa_code(user_id: str, code: str) -> bool:
    """Helper used in login flow"""
    try:
        factors = supabase.auth.mfa.list_user_factors()
        totp = next((f for f in factors if f.factor_type == "totp"), None)
        if not totp:
            return False
        verified = supabase.auth.mfa.verify({"factor_id": totp.id, "code": code})
        if verified:
            publish_event("auth.events", {"event": "2fa_verified", "user_id": user_id})
        return bool(verified)
    except Exception:
        return False