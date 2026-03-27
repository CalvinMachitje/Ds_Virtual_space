# Main entry point for the Admin Service FastAPI application.
# services/admin-service/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.dependencies.rate_limiter import limiter
from app.routes.admin import router as admin_router
from app.services.supabase_service import supabase
from app.utils.event_bus import publish_event

@asynccontextmanager
async def lifespan(app: FastAPI):
    publish_event("admin.events", {"event": "service_started", "service": "admin-service"})
    yield
    publish_event("admin.events", {"event": "service_stopped", "service": "admin-service"})

app = FastAPI(
    title="Admin Service",
    version="1.0",
    lifespan=lifespan
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, lambda req, exc: JSONResponse(
    status_code=429, content={"detail": "Rate limit exceeded"}
))

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_router, prefix="/api")

@app.get("/api/health")
async def health():
    return supabase.check_connection()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5002, reload=True)