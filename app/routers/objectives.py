"""Objectives router.

GET    /objectives                             — paginated list
GET    /objectives/:id                         — single objective
POST   /objectives                             — create
PATCH  /objectives/:id                         — update fields
PATCH  /objectives/:id/status                  — change status (also fires socket)
DELETE /objectives/:id                         — delete
POST   /objectives/:id/comments                — add comment
PATCH  /objectives/:id/subtasks/:subTaskId/toggle — toggle subtask
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

VALID_STATUSES = {"todo", "in-progress", "in-review", "completed"}


class CreateObjectiveBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    status: str = "todo"
    assignee: str
    dueDate: str | None = None


class UpdateObjectiveBody(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    assignee: str | None = None
    dueDate: str | None = None


class UpdateStatusBody(BaseModel):
    status: str


class AddCommentBody(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _populate_objective(doc: dict, db) -> dict:
    """Replace assignee_id with full user object; populate comment authors."""
    obj = serialize_doc(doc)

    # Populate assignee
    assignee_doc = await db.users.find_one({"_id": ObjectId(obj["assignee_id"])})
    obj["assignee"] = _safe_user(serialize_doc(assignee_doc)) if assignee_doc else None
    obj.pop("assignee_id", None)

    # Populate comment authors
    for comment in obj.get("comments", []):
        author_doc = await db.users.find_one({"_id": ObjectId(comment["author_id"])})
        comment["author"] = _safe_user(serialize_doc(author_doc)) if author_doc else None
        comment.pop("author_id", None)

    return obj


def _safe_user(user: dict | None) -> dict | None:
    if not user:
        return None
    return {k: v for k, v in user.items() if k != "password_hash"}


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("")
async def list_objectives(
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

    total = await db.objectives.count_documents({})
    docs = (
        await db.objectives.find(query)
        .sort("_id", -1)
        .limit(limit + 1)
        .to_list(limit + 1)
    )

    has_more = len(docs) > limit
    docs = docs[:limit]
    next_cursor = str(docs[-1]["_id"]) if has_more else None

    populated = [await _populate_objective(d, db) for d in docs]
    return paginated_response(data=populated, total=total, limit=limit, next_cursor=next_cursor)


@router.get("/{obj_id}")
async def get_objective(
    obj_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(obj_id):
        raise HTTPException(status_code=400, detail="Invalid objective ID")

    doc = await db.objectives.find_one({"_id": ObjectId(obj_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Objective not found")

    return await _populate_objective(doc, db)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_objective(
    body: CreateObjectiveBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if body.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {', '.join(VALID_STATUSES)}",
        )
    if not is_valid_object_id(body.assignee):
        raise HTTPException(status_code=400, detail="Invalid assignee ID")

    assignee = await db.users.find_one({"_id": ObjectId(body.assignee)})
    if not assignee:
        raise HTTPException(status_code=404, detail="Assignee not found")

    now = datetime.now(timezone.utc)
    doc = {
        "title": body.title,
        "description": body.description,
        "status": body.status,
        "assignee_id": body.assignee,
        "dueDate": body.dueDate,
        "subTasks": [],
        "comments": [],
        "created_at": now,
        "updated_at": now,
    }
    result = await db.objectives.insert_one(doc)
    doc["_id"] = result.inserted_id

    # Notify assignee (unless they created it themselves)
    if body.assignee != current_user["_id"]:
        notification_doc = {
            "recipient_id": body.assignee,
            "type": "objective_assigned",
            "message": f"{current_user['name']} assigned you a new objective: {body.title}",
            "read": False,
            "data": {"objectiveId": str(result.inserted_id)},
            "created_at": now,
        }
        notif_result = await db.notifications.insert_one(notification_doc)
        notification_doc["_id"] = notif_result.inserted_id
        await sio.emit(
            "notification",
            serialize_doc(notification_doc),
            room=f"user:{body.assignee}",
        )

    return await _populate_objective(doc, db)


@router.patch("/{obj_id}")
async def update_objective(
    obj_id: str,
    body: UpdateObjectiveBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(obj_id):
        raise HTTPException(status_code=400, detail="Invalid objective ID")

    doc = await db.objectives.find_one({"_id": ObjectId(obj_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Objective not found")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        return await _populate_objective(doc, db)

    # Validate status if provided
    if "status" in updates and updates["status"] not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status value")

    # Rename assignee → assignee_id for storage
    if "assignee" in updates:
        if not is_valid_object_id(updates["assignee"]):
            raise HTTPException(status_code=400, detail="Invalid assignee ID")
        updates["assignee_id"] = updates.pop("assignee")

    updates["updated_at"] = datetime.now(timezone.utc)
    result = await db.objectives.find_one_and_update(
        {"_id": ObjectId(obj_id)},
        {"$set": updates},
        return_document=True,
    )
    return await _populate_objective(result, db)


@router.patch("/{obj_id}/status")
async def update_status(
    obj_id: str,
    body: UpdateStatusBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(obj_id):
        raise HTTPException(status_code=400, detail="Invalid objective ID")
    if body.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {', '.join(VALID_STATUSES)}",
        )

    now = datetime.now(timezone.utc)
    result = await db.objectives.find_one_and_update(
        {"_id": ObjectId(obj_id)},
        {"$set": {"status": body.status, "updated_at": now}},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Objective not found")

    populated = await _populate_objective(result, db)

    # Broadcast status change via Socket.io (drag & drop real-time updates)
    await sio.emit("objective:updated", populated)

    # Notify assignee when status changes
    assignee_id = result.get("assignee_id")
    if assignee_id and assignee_id != current_user["_id"]:
        notification_doc = {
            "recipient_id": assignee_id,
            "type": "objective_status_changed",
            "message": f"Objective '{result['title']}' moved to {body.status}",
            "read": False,
            "data": {"objectiveId": obj_id, "status": body.status},
            "created_at": now,
        }
        notif_result = await db.notifications.insert_one(notification_doc)
        notification_doc["_id"] = notif_result.inserted_id
        await sio.emit(
            "notification",
            serialize_doc(notification_doc),
            room=f"user:{assignee_id}",
        )

    return populated


@router.delete("/{obj_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_objective(
    obj_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(obj_id):
        raise HTTPException(status_code=400, detail="Invalid objective ID")

    doc = await db.objectives.find_one({"_id": ObjectId(obj_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Objective not found")

    obj = serialize_doc(doc)
    if obj["assignee_id"] != current_user["_id"] and current_user["role"] not in (
        "manager",
        "admin",
    ):
        raise HTTPException(status_code=403, detail="Access denied")

    await db.objectives.delete_one({"_id": ObjectId(obj_id)})


@router.post("/{obj_id}/comments", status_code=status.HTTP_201_CREATED)
async def add_comment(
    obj_id: str,
    body: AddCommentBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(obj_id):
        raise HTTPException(status_code=400, detail="Invalid objective ID")

    doc = await db.objectives.find_one({"_id": ObjectId(obj_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Objective not found")

    comment = {
        "_id": ObjectId(),
        "author_id": current_user["_id"],
        "content": body.content,
        "created_at": datetime.now(timezone.utc),
    }

    await db.objectives.update_one(
        {"_id": ObjectId(obj_id)},
        {
            "$push": {"comments": comment},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )

    comment_out = serialize_doc(comment)
    comment_out["author"] = {
        k: v
        for k, v in current_user.items()
        if k in ("_id", "name", "avatar", "role")
    }
    comment_out.pop("author_id", None)
    return comment_out


@router.patch("/{obj_id}/subtasks/{subtask_id}/toggle")
async def toggle_subtask(
    obj_id: str,
    subtask_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    if not is_valid_object_id(obj_id) or not is_valid_object_id(subtask_id):
        raise HTTPException(status_code=400, detail="Invalid ID")

    doc = await db.objectives.find_one({"_id": ObjectId(obj_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Objective not found")

    subtask = next(
        (s for s in doc.get("subTasks", []) if str(s["_id"]) == subtask_id),
        None,
    )
    if not subtask:
        raise HTTPException(status_code=404, detail="Subtask not found")

    new_value = not subtask["completed"]
    await db.objectives.update_one(
        {"_id": ObjectId(obj_id), "subTasks._id": ObjectId(subtask_id)},
        {
            "$set": {
                "subTasks.$.completed": new_value,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )

    subtask_out = serialize_doc(subtask)
    subtask_out["completed"] = new_value
    return subtask_out
