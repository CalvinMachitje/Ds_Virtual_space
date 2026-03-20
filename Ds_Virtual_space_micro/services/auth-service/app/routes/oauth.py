# services/auth-service/app/routes/oauth.py
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from app.services.supabase_service import supabase
from app.utils.audit import log_action
from app.utils.event_bus import publish_event
from utils.utils import generate_tokens


router = APIRouter(prefix="/oauth", tags=["oauth"])


class OAuthStart(BaseModel):
    redirect_to: Optional[str] = None


@router.post("/{provider}")
@router.get("/{provider}/start")  # Optional: allow GET for simpler frontend links
async def start_oauth(
    provider: str,
    data: OAuthStart = None,  # Body for POST
    redirect_to: Optional[str] = Query(None, alias="redirectTo"),  # Query param for GET
    request: Request = None
):
    if provider not in ["google", "facebook"]:
        raise HTTPException(400, detail="Unsupported provider. Use 'google' or 'facebook'.")

    # Prefer body → query param → constructed default
    final_redirect_to = (
        data.redirect_to
        if data and data.redirect_to
        else redirect_to
        if redirect_to
        else f"{request.url.scheme}://{request.url.netloc}/api/auth/oauth/callback"
    )

    try:
        result = supabase.auth.sign_in_with_oauth({
            "provider": provider,
            "options": {
                "redirect_to": final_redirect_to,
                "scopes": "email profile" if provider == "google" else "email public_profile",
                "query_params": (
                    {"access_type": "offline", "prompt": "consent"}
                    if provider == "google"
                    else {}
                ),
            }
        })

        oauth_url = getattr(result, "url", None) or result.get("url")
        if not oauth_url:
            raise ValueError("No OAuth authorization URL returned from Supabase")

        publish_event("auth.events", {
            "event": "oauth_started",
            "provider": provider,
            "redirect_to": final_redirect_to,
            "ip": request.client.host
        })

        return {
            "success": True,
            "oauth_url": oauth_url,
            "provider": provider
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to initiate {provider} OAuth flow: {str(e)}"
        )


@router.get("/callback")
@router.post("/callback")
async def oauth_callback(
    code: str = Query(...),           # Most providers send ?code=xxx
    provider: str = Query(None),      # Optional: pass provider as query param
    state: Optional[str] = Query(None),  # For future CSRF/state validation
    request: Request = None
):
    if not code:
        raise HTTPException(400, detail="Missing authorization code")

    # You can validate 'state' here later for CSRF protection

    try:
        # This is the correct method per current Supabase Python SDK docs
        exchange_result = supabase.auth.exchange_code_for_session({"auth_code": code})

        if not exchange_result or not exchange_result.user:
            raise ValueError("OAuth code exchange failed - no user returned")

        user = exchange_result.user
        # session = exchange_result.session  # available if you need it

        # Fetch or create profile
        profile_res = supabase.table("profiles").select("*").eq("id", user.id).maybe_single().execute()
        profile = profile_res.data

        if not profile:
            # Fallback name logic (improved)
            metadata = user.user_metadata or {}
            full_name = (
                metadata.get("full_name")
                or metadata.get("name")
                or f"{metadata.get('given_name', '')} {metadata.get('family_name', '')}".strip()
                or user.email.split("@")[0].title()
            )

            new_profile = {
                "id": user.id,
                "email": user.email,
                "full_name": full_name,
                "avatar_url": metadata.get("avatar_url") or metadata.get("picture"),
                "role": "buyer",  # default — you might want to ask user later
                "created_at": "now()",
                "updated_at": "now()"
            }

            supabase.table("profiles").insert(new_profile).execute()

            publish_event("auth.events", {
                "event": "oauth_user_registered",
                "user_id": user.id,
                "provider": provider,
                "email": user.email
            })

            profile = new_profile

        # Generate your app's tokens
        access_token, refresh_token = generate_tokens(str(user.id))

        publish_event("auth.events", {
            "event": "oauth_login_success",
            "user_id": user.id,
            "provider": provider or "unknown",
            "email": user.email
        })

        log_action(
            user.id,
            "oauth_login",
            {"provider": provider or "unknown", "ip": request.client.host}
        )

        return {
            "success": True,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user": {
                **profile,
                "email": user.email,
                "email_confirmed": bool(user.email_confirmed_at)
            }
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"OAuth callback processing failed: {str(e)}"
        )