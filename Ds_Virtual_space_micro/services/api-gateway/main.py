# services/api-gateway/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import socketio
import httpx

from app.core.config import settings
from app.middleware.auth_middleware import verify_jwt, get_current_user
from app.routes.gateway import router as gateway_router

# Initialize Socket.IO server
sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins="*",
    logger=True,
    engineio_logger=True
)

# Socket.IO ASGI app
socket_app = socketio.ASGIApp(sio, other_asgi_app=None)

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 API Gateway started on port 5000")
    print("🔌 Socket.IO server initialized")
    yield
    print("👋 API Gateway shutting down")


app = FastAPI(
    title="D's Virtual Space API Gateway",
    description="Unified entry point for all microservices",
    version="1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include HTTP gateway routes
app.include_router(gateway_router)

# Mount Socket.IO
app.mount("/socket.io", socket_app)

# Health check
@app.get("/health")
async def health():
    return {"status": "healthy", "service": "api-gateway", "version": "1.0"}


# ====================== SOCKET.IO AUTHENTICATION ======================
@sio.event
async def connect(sid: str, environ: dict, auth: dict = None):
    try:
        # Try to get token from auth object (recommended way)
        token = None
        if auth and isinstance(auth, dict):
            token = auth.get("token")
        
        # Fallback: get from query string
        if not token:
            query_string = environ.get("QUERY_STRING", "")
            if "token=" in query_string:
                token = query_string.split("token=")[1].split("&")[0]

        if not token:
            print(f"Socket connect rejected - no token for sid {sid}")
            return False

        # Verify JWT
        payload = await verify_jwt(token)
        if not payload:
            print(f"Socket connect rejected - invalid token for sid {sid}")
            return False

        # Attach user info to socket session
        user_info = {
            "user_id": payload.get("sub"),
            "role": payload.get("role"),
            "admin_level": payload.get("admin_level")
        }
        
        await sio.save_session(sid, user_info)
        print(f"Socket connected: {sid} | Role: {user_info['role']}")
        
        # Join admin room if admin
        if user_info["role"] == "admin":
            await sio.enter_room(sid, "admin_room")
            
        return True

    except Exception as e:
        print(f"Socket auth error: {e}")
        return False


@sio.event
async def disconnect(sid: str):
    print(f"Socket disconnected: {sid}")


# Optional: Example admin-only event
@sio.event
async def admin_event(sid: str, data: dict):
    session = await sio.get_session(sid)
    if session.get("role") != "admin":
        return {"error": "Admin only"}
    print(f"Admin event from {session['user_id']}: {data}")
    # broadcast to all admins
    await sio.emit("admin_broadcast", data, room="admin_room")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000, reload=True)