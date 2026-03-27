# services/api-gateway/app/routes/gateway.py
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse
import httpx

from app.middleware.auth_middleware import get_current_user, get_current_admin

router = APIRouter()

# Service registry (campus lab friendly - localhost)
SERVICES = {
    "auth": "http://localhost:5001",
    "admin": "http://localhost:5002",
    "users": "http://localhost:5003",
    "support": "http://localhost:5004",
}

PUBLIC_PATHS = [
    "/auth/login",
    "/auth/signup",
    "/auth/refresh",
    "/auth/admin/login",
    "/auth/oauth",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json"
]


def is_public_path(path: str) -> bool:
    return any(path.startswith(p) for p in PUBLIC_PATHS)


async def proxy_request(service: str, path: str, request: Request):
    """Core proxy logic - correctly forwards /api/admin/* to admin-service"""
    if service not in SERVICES:
        raise HTTPException(status_code=404, detail="Service not found")

    # Special handling for admin service - keep the /admin prefix
    if service == "admin":
        target_url = f"{SERVICES[service]}/api/admin/{path}"
    else:
        target_url = f"{SERVICES[service]}/api/{path}"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)  # Let httpx handle it

    body = await request.body()

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
                follow_redirects=True,
            )

            return StreamingResponse(
                resp.aiter_bytes(),
                status_code=resp.status_code,
                headers=dict(resp.headers),
                media_type=resp.headers.get("content-type"),
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=503, detail=f"Service {service} unavailable")


# Main gateway route - catches everything
@router.api_route("/{service}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def gateway(service: str, path: str, request: Request):
    full_path = f"/{service}/{path}"

    # Public routes bypass auth
    if is_public_path(full_path):
        return await proxy_request(service, path, request)

    # Protected routes
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")

    token = auth_header.split(" ")[1]
    user = await get_current_user(token)   # We'll adjust the dependency

    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Admin-only protection
    if service == "admin" and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    return await proxy_request(service, path, request)