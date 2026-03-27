"""Shared Socket.io server instance.

Using an AsyncServer with the 'asgi' async_mode so it integrates cleanly
with FastAPI via socketio.ASGIApp.  All routers import `sio` from here to
emit events without creating circular dependencies.
"""

import socketio

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",  # tightened per-origin in main.py via FastAPI CORS
    logger=False,
    engineio_logger=False,
)
