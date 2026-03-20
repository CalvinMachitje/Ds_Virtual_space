# services/auth-service/main.py
import datetime
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.core.config import settings
from app.dependencies.rate_limiter import limiter
from app.routes.admin import router as admin_router
from app.routes.auth import router as auth_router
from app.routes.oauth import router as oauth_router
from app.routes.twofa import router as twofa_router
from app.services.supabase_service import supabase
from app.utils.event_bus import publish_event


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    publish_event("auth.events", {
        "event": "service_started",
        "service": "auth-service",
        "timestamp": os.getenv("START_TIME", "unknown")
    })
    yield
    # Shutdown
    publish_event("auth.events", {
        "event": "service_stopped",
        "service": "auth-service"
    })


app = FastAPI(
    title="D's Virtual Space - Auth Service",
    description="Authentication & Authorization microservice",
    version="2.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {"name": "auth", "description": "User authentication endpoints"},
        {"name": "admin", "description": "Admin authentication & management"},
        {"name": "oauth", "description": "OAuth providers (Google, Facebook)"},
        {"name": "2fa", "description": "Two-Factor Authentication"}
    ]
)

# Rate limiting middleware & exception handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, lambda request, exc: JSONResponse(
    status_code=429,
    content={"detail": "Rate limit exceeded. Try again later."}
))

# CORS middleware (using your settings)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all routers under /api/auth
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(admin_router, prefix="/api/auth", tags=["admin"])
app.include_router(oauth_router, prefix="/api/auth", tags=["oauth"])
app.include_router(twofa_router, prefix="/api/auth", tags=["2fa"])


@app.get("/api/health", tags=["health"])
async def health_check():
    """
    Health check endpoint.
    Returns Supabase & Redis status.
    """
    health_data = supabase.check_connection()

    # Optional: add Redis ping if you have redis_client exposed
    redis_status = "unknown"
    try:
        from app.utils.redis_utils import redis_client
        if redis_client and redis_client.ping():
            redis_status = "ok"
        else:
            redis_status = "not connected"
    except Exception:
        redis_status = "error"

    return {
        "status": "healthy" if "ok" in health_data.values() else "degraded",
        "supabase": health_data.get("supabase", "unknown"),
        "redis": redis_status,
        "service": "auth-service",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }


# Optional: global exception handler (catches unhandled errors)
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 5001))
    debug = os.getenv("DEBUG", "false").lower() == "true"

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=debug,
        log_level="debug" if debug else "info"
    )