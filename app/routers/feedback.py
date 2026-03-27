"""Feedback router.

GET    /feedback/received — paginated received feedback
GET    /feedback/sent     — paginated sent feedback
GET    /feedback/:id      — single feedback item
POST   /feedback          — create feedback
DELETE /feedback/:id      — delete feedback
"""

from datetime import datetime, timezone
from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.middleware.auth import get_current_user
from app.utils.helpers import is_valid_object_id, paginated_response, serialize_doc
from app.socket.manager import sio

router = APIRouter()

VALID_TYPES = {"positive", "constructive"}
VALID_CATEGORIES = {"communication", "leadership", "technical", "collaboration"}


class CreateFeedbackBody(BaseModel):
    to: str
    type: str
    category: str
    content: str = Field(min_length=1, max_length=2000)
    isAnonymous: bool = False
    isPublic: bool = True
    tags: list[str] = []


# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _populate_feedback(doc: dict, db, viewer_role: str) -> dict:
    """Replace user IDs with user objects; respect anonymity."""
    fb = serialize_doc(doc)

    # Populate 'to' user
    to_doc = await db.users.find_one({"_id": ObjectId(fb["to_user_id"])})
    fb["to"] = _safe_user(serialize_doc(to_doc)) if to_doc else None

    # Populate 'from' user — hide if anonymous and viewer is not manager/admin
    from_doc = await db.users.find_one({"_id": ObjectId(fb["from_user_id"])})
    if fb.get("isAnonymous") and viewer_role == "employee":
        fb["from"] = {"_id": "anonymous", "name": "Anonymous", "avatar": None}
    else:
        fb["from"] = _safe_user(serialize_doc(from_doc)) if from_doc else None

    # Remove raw IDs now that objects are populated
    fb.pop("to_user_id", None)
    fb.pop("from_user_id", None)

    return fb


def _safe_user(user: dict | None) -> dict | None:
    if not user:
        return None
    return {k: v for k, v in user.items() if k != "password_hash"}


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/received")
async def get_received(
    type: str | None = Query(None),
    category: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: Annotated[dict, Depends(get_current_user)] = None,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)] = None,
):
    query: dict = {"to_user_id": current_user["_id"]}
    if type:
        query["type"] = type
    if category:
        query["category"] = category
    if cursor:
        if not is_valid_object_id(cursor):
            raise HTTPException(status_code=400, detail="Invalid cursor")
        query["_id"] = {"$lt": ObjectId(cursor)}

    total = await db.feedback.count_documents({"to_user_id": current_user["_id"]})
    docs = (
        await db.feedback.find(query)
        .sort("_id", -1)
        .limit(limit + 1)
        .to_list(limit + 1)
    )

    has_more = len(docs) > limit
    docs = docs[:limit]
    next_cursor = str(docs[-1]["_id"]) if has_more else None

    populated = [
        await _populate_feedback(d, db, current_user["role"]) for d in docs
    ]
    return paginated_response(data=populated, total=total, limit=limit, next_cursor=next_cursor)


@router.get("/sent")
async def get_sent(
    type: str | None = Query(None),
    category: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: Annotated[dict, Depends(get_current_user)] = None,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)] = None,
):
    query: dict = {"from_user_id": current_user["_id"]}
    if type:
        query["type"] = type
    if category:
        query["category"] = category
    if cursor:
        if not is_valid_object_id(cursor):
            raise HTTPException(status_code=400, detail="Invalid cursor")
        query["_id"] = {"$lt": ObjectId(cursor)}

    total = await db.feedback.count_documents({"from_user_id": current_user["_id"]})
    docs = (
        await db.feedback.find(query)
        .sort("_id", -1)
        .limit(limit + 1)
        .to_list(limit + 1)
    )

    has_more = len(docs) > limit
    docs = docs[:limit]
    next_cursor = str(docs[-1]["_id"]) if has_more else None

    # Sender always sees themselves, so pass "admin" to bypass anonymity mask
    populated = [await _populate_feedback(d, db, "admin") for d in docs]
    return paginated_response(data=populated, total=total, limit=limit, next_cursor=next_cursor)


@router.get("/{feedback_id}")
async def get_feedback(
    feedback_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(feedback_id):
        raise HTTPException(status_code=400, detail="Invalid feedback ID")

    doc = await db.feedback.find_one({"_id": ObjectId(feedback_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Feedback not found")

    # Only sender, recipient, managers and admins can view
    fb_raw = serialize_doc(doc)
    if (
        fb_raw["from_user_id"] != current_user["_id"]
        and fb_raw["to_user_id"] != current_user["_id"]
        and current_user["role"] not in ("manager", "admin")
    ):
        raise HTTPException(status_code=403, detail="Access denied")

    return await _populate_feedback(doc, db, current_user["role"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_feedback(
    body: CreateFeedbackBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if body.type not in VALID_TYPES:
        raise HTTPException(
            status_code=400, detail=f"type must be one of: {', '.join(VALID_TYPES)}"
        )
    if body.category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of: {', '.join(VALID_CATEGORIES)}",
        )
    if not is_valid_object_id(body.to):
        raise HTTPException(status_code=400, detail="Invalid recipient ID")
    if body.to == current_user["_id"]:
        raise HTTPException(status_code=400, detail="Cannot send feedback to yourself")

    recipient = await db.users.find_one({"_id": ObjectId(body.to)})
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")

    now = datetime.now(timezone.utc)
    doc = {
        "from_user_id": current_user["_id"],
        "to_user_id": body.to,
        "type": body.type,
        "category": body.category,
        "content": body.content,
        "isAnonymous": body.isAnonymous,
        "isPublic": body.isPublic,
        "tags": body.tags,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.feedback.insert_one(doc)
    doc["_id"] = result.inserted_id

    # Create notification for recipient
    notification_doc = {
        "recipient_id": body.to,
        "type": "feedback_received",
        "message": (
            "You received anonymous feedback"
            if body.isAnonymous
            else f"{current_user['name']} sent you feedback"
        ),
        "read": False,
        "data": {"feedbackId": str(result.inserted_id)},
        "created_at": now,
    }
    notif_result = await db.notifications.insert_one(notification_doc)
    notification_doc["_id"] = notif_result.inserted_id

    # Real-time push to recipient via Socket.io
    await sio.emit(
        "notification",
        serialize_doc(notification_doc),
        room=f"user:{body.to}",
    )

    # Activity log
    await db.activity_logs.insert_one(
        {
            "user_id": current_user["_id"],
            "type": "feedback_sent",
            "message": f"{current_user['name']} sent feedback",
            "created_at": now,
        }
    )

    return await _populate_feedback(doc, db, "admin")


@router.delete("/{feedback_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feedback(
    feedback_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(feedback_id):
        raise HTTPException(status_code=400, detail="Invalid feedback ID")

    doc = await db.feedback.find_one({"_id": ObjectId(feedback_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Feedback not found")

    fb = serialize_doc(doc)
    if fb["from_user_id"] != current_user["_id"] and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    await db.feedback.delete_one({"_id": ObjectId(feedback_id)})
