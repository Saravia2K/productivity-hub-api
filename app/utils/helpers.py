from bson import ObjectId
from datetime import datetime
from typing import Any


def serialize_doc(doc: dict | None) -> dict | None:
    """Recursively convert a MongoDB document into a JSON-serialisable dict.

    - ObjectId  → str
    - datetime  → ISO-8601 string
    - nested dicts/lists are handled recursively
    """
    if doc is None:
        return None

    result: dict[str, Any] = {}
    for key, value in doc.items():
        if isinstance(value, ObjectId):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif isinstance(value, list):
            result[key] = [
                serialize_doc(item) if isinstance(item, dict) else item
                for item in value
            ]
        elif isinstance(value, dict):
            result[key] = serialize_doc(value)
        else:
            result[key] = value

    return result


def paginated_response(
    data: list,
    total: int,
    limit: int,
    next_cursor: str | None,
) -> dict:
    """Build the standard paginated envelope expected by the frontend."""
    return {
        "data": data,
        "pagination": {
            "total": total,
            "page": 1,  # cursor-based — page is informational only
            "limit": limit,
            "hasMore": next_cursor is not None,
            "nextCursor": next_cursor,
        },
    }


def is_valid_object_id(value: str) -> bool:
    try:
        ObjectId(value)
        return True
    except Exception:
        return False
