"""Dashboard router.

GET /dashboard/metrics — aggregated metrics for the current user
"""

from datetime import datetime, timezone, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.middleware.auth import get_current_user

router = APIRouter()


@router.get("/metrics")
async def get_metrics(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    uid = current_user["_id"]

    # ── Pending feedback (received, unread is approximated by last 7 days) ──
    pending_feedback = await db.feedback.count_documents(
        {
            "to_user_id": uid,
            "created_at": {
                "$gte": datetime.now(timezone.utc) - timedelta(days=7)
            },
        }
    )

    # ── Completed objectives assigned to user ──
    completed_objectives = await db.objectives.count_documents(
        {"assignee_id": uid, "status": "completed"}
    )

    # ── Team satisfaction (avg of positive feedback ratio over 30 days) ──
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    total_fb = await db.feedback.count_documents(
        {"to_user_id": uid, "created_at": {"$gte": thirty_days_ago}}
    )
    positive_fb = await db.feedback.count_documents(
        {
            "to_user_id": uid,
            "type": "positive",
            "created_at": {"$gte": thirty_days_ago},
        }
    )
    team_satisfaction = round((positive_fb / total_fb * 100) if total_fb else 0)

    # ── Active team members (users active in last 30 days via activity log) ──
    active_user_ids = await db.activity_logs.distinct(
        "user_id", {"created_at": {"$gte": thirty_days_ago}}
    )
    active_team_members = len(active_user_ids)

    # ── Feedback trend — last 6 months ──
    feedback_trend = []
    for i in range(5, -1, -1):
        month_start = (datetime.now(timezone.utc) - timedelta(days=30 * i)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        month_end = (month_start + timedelta(days=32)).replace(day=1)
        month_label = month_start.strftime("%b")

        pos = await db.feedback.count_documents(
            {
                "created_at": {"$gte": month_start, "$lt": month_end},
                "type": "positive",
            }
        )
        con = await db.feedback.count_documents(
            {
                "created_at": {"$gte": month_start, "$lt": month_end},
                "type": "constructive",
            }
        )
        feedback_trend.append(
            {"month": month_label, "positive": pos, "constructive": con}
        )

    # ── Objectives by status ──
    status_counts = []
    for s in ("todo", "in-progress", "in-review", "completed"):
        count = await db.objectives.count_documents({"status": s})
        status_counts.append({"status": s, "count": count})

    # ── Recent activity (last 10 entries, last 30 days) ──
    activity_docs = (
        await db.activity_logs.find(
            {"created_at": {"$gte": thirty_days_ago}}
        )
        .sort("created_at", -1)
        .limit(10)
        .to_list(10)
    )

    recent_activity = []
    for log in activity_docs:
        user_doc = await db.users.find_one({"_id": log.get("user_id")}) if log.get("user_id") else None
        entry = {
            "id": str(log["_id"]),
            "type": log.get("type", ""),
            "message": log.get("message", ""),
            "createdAt": log["created_at"].isoformat(),
        }
        if user_doc:
            entry["user"] = {
                "_id": str(user_doc["_id"]),
                "name": user_doc.get("name", ""),
                "avatar": user_doc.get("avatar"),
            }
        recent_activity.append(entry)

    return {
        "pendingFeedback": pending_feedback,
        "completedObjectives": completed_objectives,
        "teamSatisfaction": team_satisfaction,
        "activeTeamMembers": active_team_members,
        "feedbackTrend": feedback_trend,
        "objectivesByStatus": status_counts,
        "recentActivity": recent_activity,
    }
