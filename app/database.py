from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from app.config import settings

client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def connect_db() -> None:
    global client, _db
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    _db = client[settings.DB_NAME]
    await _create_indexes()


async def disconnect_db() -> None:
    global client
    if client:
        client.close()


def get_db() -> AsyncIOMotorDatabase:
    """FastAPI dependency — returns the active database handle."""
    return _db


async def _create_indexes() -> None:
    db = _db

    # Users — unique email, used for login queries
    await db.users.create_index("email", unique=True)

    # Refresh tokens — TTL index auto-expires documents, token lookup
    await db.refresh_tokens.create_index("token", unique=True)
    await db.refresh_tokens.create_index("user_id")
    await db.refresh_tokens.create_index("expires_at", expireAfterSeconds=0)

    # Feedback — compound indexes for the two main list queries
    # (received + sent), both ordered by newest first
    await db.feedback.create_index(
        [("to_user_id", ASCENDING), ("created_at", DESCENDING)]
    )
    await db.feedback.create_index(
        [("from_user_id", ASCENDING), ("created_at", DESCENDING)]
    )

    # Objectives — status filter + created_at for ordering
    await db.objectives.create_index(
        [("assignee_id", ASCENDING), ("status", ASCENDING)]
    )
    await db.objectives.create_index([("created_at", DESCENDING)])

    # Teams — manager lookup + member lookup
    await db.teams.create_index("manager_id")
    await db.teams.create_index("members.user_id")

    # Chat messages — per-team ordered by newest
    await db.chat_messages.create_index(
        [("team_id", ASCENDING), ("created_at", DESCENDING)]
    )

    # Notifications — per-user newest first; unread filter
    await db.notifications.create_index(
        [("recipient_id", ASCENDING), ("created_at", DESCENDING)]
    )
    await db.notifications.create_index(
        [("recipient_id", ASCENDING), ("read", ASCENDING)]
    )

    # Activity log — admin panel queries last 30 days
    await db.activity_logs.create_index([("created_at", DESCENDING)])
    await db.activity_logs.create_index("user_id")
