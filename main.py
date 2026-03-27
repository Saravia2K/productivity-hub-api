"""TeamSync Feedback Hub — FastAPI backend.

Entry point for both development (uvicorn main:socket_app --reload) and
production (uvicorn main:socket_app).

Architecture
------------
FastAPI handles all REST routes under /api.
python-socketio is mounted via socketio.ASGIApp which wraps FastAPI and
intercepts Socket.IO upgrade requests at /socket.io/.
"""

import logging
import socketio

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import settings
from app.database import connect_db, disconnect_db
from app.routers import auth, dashboard, feedback, notifications, objectives, teams, users

# Import handlers so their @sio.event decorators are registered
import app.socket.handlers  # noqa: F401
from app.socket.manager import sio

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="TeamSync Feedback Hub",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# ── Rate limiter ──
limiter = Limiter(key_func=get_remote_address, default_limits=["100/15minutes"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global exception handler ──
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"message": "Internal server error", "statusCode": 500},
    )


# ── Lifecycle ──
@app.on_event("startup")
async def startup():
    await connect_db()
    logger.info("Connected to MongoDB")


@app.on_event("shutdown")
async def shutdown():
    await disconnect_db()
    logger.info("Disconnected from MongoDB")


# ── Routers ──
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(users.router, prefix="/api/users", tags=["users"])
app.include_router(feedback.router, prefix="/api/feedback", tags=["feedback"])
app.include_router(objectives.router, prefix="/api/objectives", tags=["objectives"])
app.include_router(teams.router, prefix="/api/teams", tags=["teams"])
app.include_router(notifications.router, prefix="/api/notifications", tags=["notifications"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ─── Socket.io ASGI wrapper ───────────────────────────────────────────────────
# This is the actual ASGI app to pass to uvicorn.
# Socket.IO intercepts requests to /socket.io/; everything else is forwarded to
# the FastAPI app.

socket_app = socketio.ASGIApp(sio, other_asgi_app=app, socketio_path="/socket.io")
