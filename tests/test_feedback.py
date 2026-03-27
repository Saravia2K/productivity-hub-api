"""Integration tests for feedback endpoints."""

import pytest
from httpx import AsyncClient


pytestmark = pytest.mark.asyncio


async def _register(client: AsyncClient, name: str, email: str, password: str = "pass1234") -> dict:
    resp = await client.post(
        "/api/auth/register",
        json={"name": name, "email": email, "password": password},
    )
    assert resp.status_code == 201
    return resp.json()


async def test_create_feedback(client: AsyncClient):
    sender = await _register(client, "Alice", "alice@example.com")
    recipient = await _register(client, "Bob", "bob@example.com")
    token = sender["tokens"]["accessToken"]
    recipient_id = recipient["user"]["_id"]

    resp = await client.post(
        "/api/feedback",
        json={
            "to": recipient_id,
            "type": "positive",
            "category": "communication",
            "content": "Great communicator!",
            "isAnonymous": False,
            "isPublic": True,
            "tags": ["teamwork"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["type"] == "positive"
    assert data["to"]["_id"] == recipient_id
    assert data["from"]["name"] == "Alice"


async def test_create_feedback_self(client: AsyncClient):
    user = await _register(client, "Charlie", "charlie@example.com")
    token = user["tokens"]["accessToken"]
    uid = user["user"]["_id"]

    resp = await client.post(
        "/api/feedback",
        json={
            "to": uid,
            "type": "positive",
            "category": "technical",
            "content": "Go me!",
            "isAnonymous": False,
            "isPublic": True,
            "tags": [],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


async def test_create_feedback_invalid_type(client: AsyncClient):
    sender = await _register(client, "Dan", "dan@example.com")
    recipient = await _register(client, "Ed", "ed@example.com")
    token = sender["tokens"]["accessToken"]

    resp = await client.post(
        "/api/feedback",
        json={
            "to": recipient["user"]["_id"],
            "type": "bad_type",
            "category": "technical",
            "content": "Hmm",
            "isAnonymous": False,
            "isPublic": True,
            "tags": [],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


async def test_received_feedback(client: AsyncClient):
    sender = await _register(client, "Fiona", "fiona@example.com")
    recipient = await _register(client, "Gus", "gus@example.com")
    s_token = sender["tokens"]["accessToken"]
    r_token = recipient["tokens"]["accessToken"]
    r_id = recipient["user"]["_id"]

    await client.post(
        "/api/feedback",
        json={
            "to": r_id,
            "type": "constructive",
            "category": "leadership",
            "content": "Could improve meeting structure.",
            "isAnonymous": False,
            "isPublic": False,
            "tags": [],
        },
        headers={"Authorization": f"Bearer {s_token}"},
    )

    resp = await client.get(
        "/api/feedback/received",
        headers={"Authorization": f"Bearer {r_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pagination"]["total"] == 1
    assert data["data"][0]["type"] == "constructive"


async def test_anonymous_feedback_hidden_from_employee(client: AsyncClient):
    sender = await _register(client, "Hannah", "hannah@example.com")
    recipient = await _register(client, "Ivan", "ivan@example.com")
    s_token = sender["tokens"]["accessToken"]
    r_token = recipient["tokens"]["accessToken"]
    r_id = recipient["user"]["_id"]

    await client.post(
        "/api/feedback",
        json={
            "to": r_id,
            "type": "positive",
            "category": "collaboration",
            "content": "Great teamwork.",
            "isAnonymous": True,
            "isPublic": True,
            "tags": [],
        },
        headers={"Authorization": f"Bearer {s_token}"},
    )

    resp = await client.get(
        "/api/feedback/received",
        headers={"Authorization": f"Bearer {r_token}"},
    )
    assert resp.status_code == 200
    fb = resp.json()["data"][0]
    # Recipient (employee) must see "Anonymous", not the sender's name
    assert fb["from"]["name"] == "Anonymous"


async def test_delete_feedback(client: AsyncClient):
    sender = await _register(client, "Jane", "jane@example.com")
    recipient = await _register(client, "Kyle", "kyle@example.com")
    s_token = sender["tokens"]["accessToken"]

    create_resp = await client.post(
        "/api/feedback",
        json={
            "to": recipient["user"]["_id"],
            "type": "positive",
            "category": "technical",
            "content": "Nice code.",
            "isAnonymous": False,
            "isPublic": True,
            "tags": [],
        },
        headers={"Authorization": f"Bearer {s_token}"},
    )
    fb_id = create_resp.json()["_id"]

    del_resp = await client.delete(
        f"/api/feedback/{fb_id}",
        headers={"Authorization": f"Bearer {s_token}"},
    )
    assert del_resp.status_code == 204


async def test_unauthenticated_feedback(client: AsyncClient):
    resp = await client.get("/api/feedback/received")
    assert resp.status_code == 403
