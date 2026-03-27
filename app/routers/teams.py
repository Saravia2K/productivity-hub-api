"""Teams router.

GET    /teams                      — list teams the user belongs to (or all for admin)
GET    /teams/:id                  — get team with members
POST   /teams                      — create team (manager/admin)
PATCH  /teams/:id                  — update team info (manager/admin)
POST   /teams/:id/members          — add member (manager/admin)
DELETE /teams/:id/members/:userId  — remove member (manager/admin)
GET    /teams/:id/chat             — paginated chat history
POST   /teams/:id/chat             — send chat message (broadcasts via socket)
"""

from datetime import datetime, timezone
from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.middleware.auth import get_current_user, require_manager_or_admin
from app.utils.helpers import is_valid_object_id, paginated_response, serialize_doc
from app.socket.manager import sio

router = APIRouter()


class CreateTeamBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None


class UpdateTeamBody(BaseModel):
    name: str | None = None
    description: str | None = None


class AddMemberBody(BaseModel):
    userId: str


class SendMessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _safe_user(user: dict | None) -> dict | None:
    if not user:
        return None
    return {k: v for k, v in user.items() if k != "password_hash"}


async def _populate_team(doc: dict, db) -> dict:
    team = serialize_doc(doc)

    # Populate manager
    manager_doc = await db.users.find_one({"_id": ObjectId(team["manager_id"])})
    team["manager"] = _safe_user(serialize_doc(manager_doc))
    team.pop("manager_id", None)

    # Populate each member's user object
    populated_members = []
    for member in team.get("members", []):
        user_doc = await db.users.find_one({"_id": ObjectId(member["user_id"])})
        populated_members.append(
            {
                "user": _safe_user(serialize_doc(user_doc)),
                "role": member.get("role", "employee"),
                "joinedAt": member.get("joined_at") or member.get("joinedAt"),
            }
        )
    team["members"] = populated_members

    return team


async def _populate_message(doc: dict, db) -> dict:
    msg = serialize_doc(doc)
    author_doc = await db.users.find_one({"_id": ObjectId(msg["author_id"])})
    msg["author"] = _safe_user(serialize_doc(author_doc))
    msg.pop("author_id", None)
    msg.pop("team_id", None)
    return msg


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("")
async def list_teams(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if current_user["role"] == "admin":
        docs = await db.teams.find().sort("created_at", -1).to_list(100)
    else:
        docs = (
            await db.teams.find(
                {
                    "$or": [
                        {"manager_id": current_user["_id"]},
                        {"members.user_id": current_user["_id"]},
                    ]
                }
            )
            .sort("created_at", -1)
            .to_list(100)
        )

    return [await _populate_team(d, db) for d in docs]


@router.get("/{team_id}")
async def get_team(
    team_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(team_id):
        raise HTTPException(status_code=400, detail="Invalid team ID")

    doc = await db.teams.find_one({"_id": ObjectId(team_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")

    return await _populate_team(doc, db)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_team(
    body: CreateTeamBody,
    current_user: Annotated[dict, Depends(require_manager_or_admin)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    now = datetime.now(timezone.utc)
    doc = {
        "name": body.name,
        "description": body.description,
        "manager_id": current_user["_id"],
        "members": [
            {
                "user_id": current_user["_id"],
                "role": current_user["role"],
                "joined_at": now,
            }
        ],
        "created_at": now,
        "updated_at": now,
    }
    result = await db.teams.insert_one(doc)
    doc["_id"] = result.inserted_id
    return await _populate_team(doc, db)


@router.patch("/{team_id}")
async def update_team(
    team_id: str,
    body: UpdateTeamBody,
    current_user: Annotated[dict, Depends(require_manager_or_admin)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(team_id):
        raise HTTPException(status_code=400, detail="Invalid team ID")

    doc = await db.teams.find_one({"_id": ObjectId(team_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")

    team = serialize_doc(doc)
    if team["manager_id"] != current_user["_id"] and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only the team manager can update this team")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        return await _populate_team(doc, db)

    updates["updated_at"] = datetime.now(timezone.utc)
    result = await db.teams.find_one_and_update(
        {"_id": ObjectId(team_id)}, {"$set": updates}, return_document=True
    )
    return await _populate_team(result, db)


@router.post("/{team_id}/members")
async def add_member(
    team_id: str,
    body: AddMemberBody,
    current_user: Annotated[dict, Depends(require_manager_or_admin)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(team_id) or not is_valid_object_id(body.userId):
        raise HTTPException(status_code=400, detail="Invalid ID")

    doc = await db.teams.find_one({"_id": ObjectId(team_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")

    team = serialize_doc(doc)
    if team["manager_id"] != current_user["_id"] and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only the team manager can add members")

    # Check user exists
    user_doc = await db.users.find_one({"_id": ObjectId(body.userId)})
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent duplicates
    existing = [m for m in doc.get("members", []) if str(m["user_id"]) == body.userId]
    if existing:
        raise HTTPException(status_code=409, detail="User already in team")

    new_member = {
        "user_id": body.userId,
        "role": serialize_doc(user_doc)["role"],
        "joined_at": datetime.now(timezone.utc),
    }
    result = await db.teams.find_one_and_update(
        {"_id": ObjectId(team_id)},
        {
            "$push": {"members": new_member},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
        return_document=True,
    )
    return await _populate_team(result, db)


@router.delete("/{team_id}/members/{user_id}")
async def remove_member(
    team_id: str,
    user_id: str,
    current_user: Annotated[dict, Depends(require_manager_or_admin)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(team_id) or not is_valid_object_id(user_id):
        raise HTTPException(status_code=400, detail="Invalid ID")

    doc = await db.teams.find_one({"_id": ObjectId(team_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")

    team = serialize_doc(doc)
    if team["manager_id"] != current_user["_id"] and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    result = await db.teams.find_one_and_update(
        {"_id": ObjectId(team_id)},
        {
            "$pull": {"members": {"user_id": user_id}},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
        return_document=True,
    )
    return await _populate_team(result, db)


# ─── Chat ─────────────────────────────────────────────────────────────────────


@router.get("/{team_id}/chat")
async def get_chat(
    team_id: str,
    cursor: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    current_user: Annotated[dict, Depends(get_current_user)] = None,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)] = None,
):
    if not is_valid_object_id(team_id):
        raise HTTPException(status_code=400, detail="Invalid team ID")

    query: dict = {"team_id": team_id}
    if cursor:
        if not is_valid_object_id(cursor):
            raise HTTPException(status_code=400, detail="Invalid cursor")
        query["_id"] = {"$lt": ObjectId(cursor)}

    total = await db.chat_messages.count_documents({"team_id": team_id})
    docs = (
        await db.chat_messages.find(query)
        .sort("_id", -1)
        .limit(limit + 1)
        .to_list(limit + 1)
    )

    has_more = len(docs) > limit
    docs = docs[:limit]
    next_cursor = str(docs[-1]["_id"]) if has_more else None

    populated = [await _populate_message(d, db) for d in docs]
    # Return in chronological order for the chat view
    populated.reverse()

    return paginated_response(
        data=populated, total=total, limit=limit, next_cursor=next_cursor
    )


@router.post("/{team_id}/chat", status_code=status.HTTP_201_CREATED)
async def send_message(
    team_id: str,
    body: SendMessageBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(team_id):
        raise HTTPException(status_code=400, detail="Invalid team ID")

    team_doc = await db.teams.find_one({"_id": ObjectId(team_id)})
    if not team_doc:
        raise HTTPException(status_code=404, detail="Team not found")

    now = datetime.now(timezone.utc)
    doc = {
        "team_id": team_id,
        "author_id": current_user["_id"],
        "content": body.content,
        "created_at": now,
    }
    result = await db.chat_messages.insert_one(doc)
    doc["_id"] = result.inserted_id

    msg_out = await _populate_message(doc, db)

    # Broadcast to everyone in the team room (include teamId so clients can filter)
    await sio.emit("chat:message", {**msg_out, "teamId": team_id}, room=f"team:{team_id}")

    return msg_out
