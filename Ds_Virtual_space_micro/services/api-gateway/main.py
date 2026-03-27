# services/api-gateway/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

from app.core.config import settings
from app.middleware.auth_middleware import get_current_user, get_current_admin
from app.routes.gateway import router as gateway_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 API Gateway started on port 5000")
    yield
    print("👋 API Gateway shutting down")


app = FastAPI(
    title="D's Virtual Space API Gateway",
    description="Unified entry point for all microservices",
    version="1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include gateway routes
app.include_router(gateway_router)

# Health check
@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "api-gateway",
        "version": "1.0"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000, reload=True)