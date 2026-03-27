"""Notifications router.

GET   /notifications             — paginated list for current user
PATCH /notifications/read-all    — mark all as read
PATCH /notifications/:id/read    — mark one as read
DELETE /notifications/:id        — delete one
"""

from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.middleware.auth import get_current_user
from app.utils.helpers import is_valid_object_id, paginated_response, serialize_doc

router = APIRouter()


@router.get("")
async def list_notifications(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: Annotated[dict, Depends(get_current_user)] = None,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)] = None,
):
    query: dict = {"recipient_id": current_user["_id"]}
    if cursor:
        if not is_valid_object_id(cursor):
            raise HTTPException(status_code=400, detail="Invalid cursor")
        query["_id"] = {"$lt": ObjectId(cursor)}

    total = await db.notifications.count_documents(
        {"recipient_id": current_user["_id"]}
    )
    docs = (
        await db.notifications.find(query)
        .sort("_id", -1)
        .limit(limit + 1)
        .to_list(limit + 1)
    )

    has_more = len(docs) > limit
    docs = docs[:limit]
    next_cursor = str(docs[-1]["_id"]) if has_more else None

    return paginated_response(
        data=[serialize_doc(d) for d in docs],
        total=total,
        limit=limit,
        next_cursor=next_cursor,
    )


@router.patch("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    await db.notifications.update_many(
        {"recipient_id": current_user["_id"], "read": False},
        {"$set": {"read": True}},
    )


@router.patch("/{notif_id}/read")
async def mark_read(
    notif_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(notif_id):
        raise HTTPException(status_code=400, detail="Invalid notification ID")

    result = await db.notifications.find_one_and_update(
        {"_id": ObjectId(notif_id), "recipient_id": current_user["_id"]},
        {"$set": {"read": True}},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Notification not found")

    return serialize_doc(result)


@router.delete("/{notif_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification(
    notif_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(notif_id):
        raise HTTPException(status_code=400, detail="Invalid notification ID")

    result = await db.notifications.find_one(
        {"_id": ObjectId(notif_id), "recipient_id": current_user["_id"]}
    )
    if not result:
        raise HTTPException(status_code=404, detail="Notification not found")

    await db.notifications.delete_one({"_id": ObjectId(notif_id)})
