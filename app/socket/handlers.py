"""Socket.io event handlers.

Authentication flow
-------------------
The frontend calls ``connectSocket(accessToken)`` which passes ``{ token }``
in ``socket.auth``.  On connect, we decode the JWT and join the socket into
two rooms:
  • ``user:<user_id>``  — for personal notifications
  • ``team:<team_id>``  — joined for every team the user belongs to

Events emitted by the server
-----------------------------
notification          → to ``user:<id>``   when feedback/objective events occur
objective:updated     → broadcast           when an objective status changes
chat:message          → to ``team:<id>``    when a chat message is posted

Events listened from the client
---------------------------------
join:team   { teamId }   — join a specific team room (validated server-side)
leave:team  { teamId }   — leave a team room
"""

import logging
from bson import ObjectId
from jose import JWTError, jwt

from app.config import settings
from app.database import get_db
from app.socket.manager import sio

logger = logging.getLogger(__name__)


# ─── Connection lifecycle ─────────────────────────────────────────────────────


@sio.event
async def connect(sid: str, environ: dict, auth: dict | None):
    """Authenticate via JWT and join personal + team rooms."""
    token = (auth or {}).get("token", "")
    if not token:
        logger.warning("Socket connection refused: no token (sid=%s)", sid)
        return False  # refuse connection

    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
        if payload.get("type") != "access":
            return False
        user_id: str = payload.get("sub", "")
    except JWTError:
        logger.warning("Socket connection refused: invalid token (sid=%s)", sid)
        return False

    # Store user_id in the session so we can reference it on disconnect
    await sio.save_session(sid, {"user_id": user_id})

    # Join personal notification room
    await sio.enter_room(sid, f"user:{user_id}")

    # Join all team rooms this user belongs to
    db = get_db()
    if db is not None:
        try:
            team_docs = await db.teams.find(
                {
                    "$or": [
                        {"manager_id": user_id},
                        {"members.user_id": user_id},
                    ]
                },
                {"_id": 1},
            ).to_list(50)
            for team in team_docs:
                await sio.enter_room(sid, f"team:{str(team['_id'])}")
        except Exception:
            logger.exception("Failed to join team rooms for user %s", user_id)

    logger.info("Socket connected: sid=%s user=%s", sid, user_id)


@sio.event
async def disconnect(sid: str):
    session = await sio.get_session(sid)
    user_id = (session or {}).get("user_id", "unknown")
    logger.info("Socket disconnected: sid=%s user=%s", sid, user_id)


# ─── Team room management (client-driven) ────────────────────────────────────


@sio.event
async def join_team(sid: str, data: dict):
    """Client requests to join a specific team chat room."""
    team_id = (data or {}).get("teamId", "")
    if not team_id:
        return

    session = await sio.get_session(sid)
    user_id = (session or {}).get("user_id")
    if not user_id:
        return

    # Verify the user actually belongs to this team before joining
    db = get_db()
    if db is not None:
        try:
            team = await db.teams.find_one(
                {
                    "_id": ObjectId(team_id),
                    "$or": [
                        {"manager_id": user_id},
                        {"members.user_id": user_id},
                    ],
                }
            )
            if team:
                await sio.enter_room(sid, f"team:{team_id}")
        except Exception:
            pass


@sio.event
async def leave_team(sid: str, data: dict):
    """Client explicitly leaves a team room."""
    team_id = (data or {}).get("teamId", "")
    if team_id:
        await sio.leave_room(sid, f"team:{team_id}")
