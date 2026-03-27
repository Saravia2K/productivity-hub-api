"""Users router.

GET   /users          — list all users (paginated, admin only)
GET   /users/:id      — get single user
PATCH /users/:id/role — change role (admin only)
"""

from datetime import datetime, timezone
from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.database import get_db
from app.middleware.auth import get_current_user, require_admin
from app.utils.helpers import is_valid_object_id, paginated_response, serialize_doc

router = APIRouter()

VALID_ROLES = {"admin", "manager", "employee"}


class UpdateRoleBody(BaseModel):
    role: str


def _safe_user(user: dict) -> dict:
    return {k: v for k, v in user.items() if k != "password_hash"}


@router.get("")
async def list_users(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: Annotated[dict, Depends(get_current_user)] = None,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)] = None,
):
    query: dict = {}
    if cursor:
        if not is_valid_object_id(cursor):
            raise HTTPException(status_code=400, detail="Invalid cursor")
        query["_id"] = {"$lt": ObjectId(cursor)}

    total = await db.users.count_documents({})
    docs = (
        await db.users.find(query).sort("_id", -1).limit(limit + 1).to_list(limit + 1)
    )

    has_more = len(docs) > limit
    docs = docs[:limit]
    next_cursor = str(docs[-1]["_id"]) if has_more else None

    return paginated_response(
        data=[_safe_user(serialize_doc(d)) for d in docs],
        total=total,
        limit=limit,
        next_cursor=next_cursor,
    )


@router.get("/{user_id}")
async def get_user(
    user_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    doc = await db.users.find_one({"_id": ObjectId(user_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")

    return _safe_user(serialize_doc(doc))


@router.patch("/{user_id}/role")
async def update_role(
    user_id: str,
    body: UpdateRoleBody,
    admin: Annotated[dict, Depends(require_admin)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Role must be one of: {', '.join(VALID_ROLES)}",
        )
    if not is_valid_object_id(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    result = await db.users.find_one_and_update(
        {"_id": ObjectId(user_id)},
        {"$set": {"role": body.role, "updated_at": datetime.now(timezone.utc)}},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="User not found")

    await db.activity_logs.insert_one(
        {
            "user_id": admin["_id"],
            "type": "role_change",
            "message": f"Admin changed {result['name']}'s role to {body.role}",
            "created_at": datetime.now(timezone.utc),
        }
    )

    return _safe_user(serialize_doc(result))
