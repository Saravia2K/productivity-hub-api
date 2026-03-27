"""Integration tests for auth endpoints."""

import pytest
from httpx import AsyncClient


pytestmark = pytest.mark.asyncio


async def test_register_success(client: AsyncClient):
    resp = await client.post(
        "/api/auth/register",
        json={
            "name": "Alice",
            "email": "alice@example.com",
            "password": "secret123",
            "department": "Engineering",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["user"]["email"] == "alice@example.com"
    assert data["user"]["role"] == "employee"
    assert "password_hash" not in data["user"]
    assert "accessToken" in data["tokens"]
    assert "refreshToken" in data["tokens"]


async def test_register_duplicate_email(client: AsyncClient):
    payload = {"name": "Alice", "email": "alice@example.com", "password": "secret123"}
    await client.post("/api/auth/register", json=payload)
    resp = await client.post("/api/auth/register", json=payload)
    assert resp.status_code == 409


async def test_register_invalid_email(client: AsyncClient):
    resp = await client.post(
        "/api/auth/register",
        json={"name": "X", "email": "not-an-email", "password": "secret123"},
    )
    assert resp.status_code == 422


async def test_login_success(client: AsyncClient):
    await client.post(
        "/api/auth/register",
        json={"name": "Bob", "email": "bob@example.com", "password": "pass1234"},
    )
    resp = await client.post(
        "/api/auth/login",
        json={"email": "bob@example.com", "password": "pass1234"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["email"] == "bob@example.com"
    assert "accessToken" in data["tokens"]


async def test_login_wrong_password(client: AsyncClient):
    await client.post(
        "/api/auth/register",
        json={"name": "Carol", "email": "carol@example.com", "password": "correct"},
    )
    resp = await client.post(
        "/api/auth/login",
        json={"email": "carol@example.com", "password": "wrong"},
    )
    assert resp.status_code == 401


async def test_login_unknown_user(client: AsyncClient):
    resp = await client.post(
        "/api/auth/login",
        json={"email": "ghost@example.com", "password": "any"},
    )
    assert resp.status_code == 401


async def test_get_me(client: AsyncClient):
    reg = await client.post(
        "/api/auth/register",
        json={"name": "Dave", "email": "dave@example.com", "password": "pass1234"},
    )
    token = reg.json()["tokens"]["accessToken"]

    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "dave@example.com"


async def test_get_me_unauthenticated(client: AsyncClient):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 403  # HTTPBearer returns 403 when no credentials


async def test_refresh_token(client: AsyncClient):
    reg = await client.post(
        "/api/auth/register",
        json={"name": "Eve", "email": "eve@example.com", "password": "pass1234"},
    )
    refresh_token = reg.json()["tokens"]["refreshToken"]

    resp = await client.post("/api/auth/refresh", json={"refreshToken": refresh_token})
    assert resp.status_code == 200
    assert "accessToken" in resp.json()
    assert "refreshToken" in resp.json()


async def test_update_profile(client: AsyncClient):
    reg = await client.post(
        "/api/auth/register",
        json={"name": "Frank", "email": "frank@example.com", "password": "pass1234"},
    )
    token = reg.json()["tokens"]["accessToken"]

    resp = await client.patch(
        "/api/auth/me",
        json={"bio": "I love feedback", "department": "Product"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["bio"] == "I love feedback"
    assert data["department"] == "Product"
