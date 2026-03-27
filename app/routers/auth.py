"""Authentication router.

POST /auth/register
POST /auth/login
POST /auth/logout
POST /auth/refresh
GET  /auth/me
PATCH /auth/me
PATCH /auth/me/password
GET  /auth/google
GET  /auth/google/callback
"""

from datetime import datetime, timezone, timedelta
from typing import Annotated

import httpx
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field

from app.config import settings
from app.database import get_db
from app.middleware.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
)
from app.utils.helpers import serialize_doc

router = APIRouter()
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ─── Schemas ──────────────────────────────────────────────────────────────────


class RegisterBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=6)
    department: str | None = None


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class RefreshBody(BaseModel):
    refreshToken: str


class UpdateProfileBody(BaseModel):
    name: str | None = None
    bio: str | None = None
    department: str | None = None
    avatar: str | None = None


class ChangePasswordBody(BaseModel):
    currentPassword: str
    newPassword: str = Field(min_length=6)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _user_response(user: dict) -> dict:
    """Strip password_hash before returning user to client."""
    return {k: v for k, v in user.items() if k != "password_hash"}


async def _store_refresh_token(db, user_id: str, token: str) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    await db.refresh_tokens.insert_one(
        {
            "user_id": user_id,
            "token": token,
            "expires_at": expires_at,
            "created_at": datetime.now(timezone.utc),
        }
    )


def _build_tokens(user: dict) -> dict:
    access = create_access_token(user["_id"], user["role"])
    refresh = create_refresh_token(user["_id"])
    return {"accessToken": access, "refreshToken": refresh}


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterBody,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    existing = await db.users.find_one({"email": body.email})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    now = datetime.now(timezone.utc)
    user_doc = {
        "email": body.email,
        "name": body.name,
        "password_hash": pwd_ctx.hash(body.password),
        "avatar": None,
        "bio": None,
        "department": body.department,
        "role": "employee",
        "emailVerified": False,
        "notificationPreferences": {"email": True, "inApp": True},
        "created_at": now,
        "updated_at": now,
    }
    result = await db.users.insert_one(user_doc)
    user_doc["_id"] = result.inserted_id

    user = serialize_doc(user_doc)
    tokens = _build_tokens(user)
    await _store_refresh_token(db, user["_id"], tokens["refreshToken"])

    await db.activity_logs.insert_one(
        {
            "user_id": user["_id"],
            "type": "register",
            "message": f"{user['name']} joined the platform",
            "created_at": datetime.now(timezone.utc),
        }
    )

    return {"user": _user_response(user), "tokens": tokens}


@router.post("/login")
async def login(
    body: LoginBody,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    user_doc = await db.users.find_one({"email": body.email})
    if not user_doc or not pwd_ctx.verify(
        body.password, user_doc.get("password_hash", "")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    user = serialize_doc(user_doc)
    tokens = _build_tokens(user)
    await _store_refresh_token(db, user["_id"], tokens["refreshToken"])

    await db.activity_logs.insert_one(
        {
            "user_id": user["_id"],
            "type": "login",
            "message": f"{user['name']} logged in",
            "created_at": datetime.now(timezone.utc),
        }
    )

    return {"user": _user_response(user), "tokens": tokens}


@router.post("/logout")
async def logout(
    body: RefreshBody,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    await db.refresh_tokens.delete_one({"token": body.refreshToken})
    return {"message": "Logged out"}


@router.post("/refresh")
async def refresh_token(
    body: RefreshBody,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    payload = decode_token(body.refreshToken)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")

    stored = await db.refresh_tokens.find_one({"token": body.refreshToken})
    if not stored:
        raise HTTPException(status_code=401, detail="Refresh token not found or expired")

    user_id = payload["sub"]
    user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user_doc:
        raise HTTPException(status_code=401, detail="User not found")

    user = serialize_doc(user_doc)

    # Rotate refresh token
    await db.refresh_tokens.delete_one({"token": body.refreshToken})
    tokens = _build_tokens(user)
    await _store_refresh_token(db, user["_id"], tokens["refreshToken"])

    return tokens


@router.get("/me")
async def get_me(
    current_user: Annotated[dict, Depends(get_current_user)],
):
    return _user_response(current_user)


@router.patch("/me")
async def update_profile(
    body: UpdateProfileBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return _user_response(current_user)

    updates["updated_at"] = datetime.now(timezone.utc)
    await db.users.update_one(
        {"_id": ObjectId(current_user["_id"])}, {"$set": updates}
    )
    updated = await db.users.find_one({"_id": ObjectId(current_user["_id"])})
    return _user_response(serialize_doc(updated))


@router.patch("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    user_doc = await db.users.find_one({"_id": ObjectId(current_user["_id"])})
    if not pwd_ctx.verify(body.currentPassword, user_doc.get("password_hash", "")):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    await db.users.update_one(
        {"_id": ObjectId(current_user["_id"])},
        {
            "$set": {
                "password_hash": pwd_ctx.hash(body.newPassword),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


# ─── Google OAuth ─────────────────────────────────────────────────────────────


@router.get("/google")
async def google_oauth_url():
    """Return the Google OAuth authorization URL for the frontend to redirect to."""
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")

    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return {"url": f"https://accounts.google.com/o/oauth2/v2/auth?{query}"}


class GoogleCallbackBody(BaseModel):
    code: str


@router.post("/google/callback")
async def google_oauth_callback(
    body: GoogleCallbackBody,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Exchange a Google OAuth code for app tokens. The frontend sends the code
    it received on its callback page after the user authenticated with Google."""
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")

    async with httpx.AsyncClient() as http_client:
        token_resp = await http_client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": body.code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        token_data = token_resp.json()
        if "error" in token_data:
            raise HTTPException(status_code=400, detail=token_data["error"])

        userinfo_resp = await http_client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        profile = userinfo_resp.json()

    email = profile.get("email")
    now = datetime.now(timezone.utc)

    user_doc = await db.users.find_one({"email": email})
    if not user_doc:
        new_user = {
            "email": email,
            "name": profile.get("name", email),
            "password_hash": "",
            "avatar": profile.get("picture"),
            "bio": None,
            "department": None,
            "role": "employee",
            "emailVerified": True,
            "notificationPreferences": {"email": True, "inApp": True},
            "created_at": now,
            "updated_at": now,
        }
        result = await db.users.insert_one(new_user)
        new_user["_id"] = result.inserted_id
        user_doc = new_user

    user = serialize_doc(user_doc)
    tokens = _build_tokens(user)
    await _store_refresh_token(db, user["_id"], tokens["refreshToken"])

    return {"user": _user_response(user), "tokens": tokens}
