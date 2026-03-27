"""Shared pytest fixtures for integration tests.

Uses mongomock-motor for an in-memory MongoDB so tests run without a real DB.
Install:  pip install mongomock-motor
"""

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch, MagicMock

# ── Override settings before anything else imports them ──────────────────────
import os
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "teamsync_test")
os.environ.setdefault("JWT_SECRET", "test-secret-key")

from main import app
import app.database as db_module


@pytest_asyncio.fixture(scope="function")
async def mock_db():
    """Return a dict-based in-memory DB mock for unit tests."""
    collections: dict = {}

    class FakeCollection:
        def __init__(self, name):
            self.name = name
            self._docs: list = []

        async def insert_one(self, doc):
            from bson import ObjectId
            if "_id" not in doc:
                doc["_id"] = ObjectId()
            self._docs.append(doc)
            result = MagicMock()
            result.inserted_id = doc["_id"]
            return result

        async def find_one(self, query):
            for doc in self._docs:
                if self._match(doc, query):
                    return doc
            return None

        def find(self, query=None):
            matched = [d for d in self._docs if self._match(d, query or {})]
            return FakeCursor(matched)

        async def count_documents(self, query):
            return sum(1 for d in self._docs if self._match(d, query))

        async def update_one(self, query, update, **kwargs):
            for doc in self._docs:
                if self._match(doc, query):
                    self._apply_update(doc, update)
                    break
            result = MagicMock()
            result.modified_count = 1
            return result

        async def find_one_and_update(self, query, update, **kwargs):
            for doc in self._docs:
                if self._match(doc, query):
                    self._apply_update(doc, update)
                    return doc
            return None

        async def update_many(self, query, update):
            for doc in self._docs:
                if self._match(doc, query):
                    self._apply_update(doc, update)

        async def delete_one(self, query):
            for i, doc in enumerate(self._docs):
                if self._match(doc, query):
                    self._docs.pop(i)
                    break

        async def create_index(self, *args, **kwargs):
            pass

        async def distinct(self, field, query=None):
            return list({d.get(field) for d in self._docs if self._match(d, query or {}) and d.get(field)})

        def _match(self, doc, query):
            from bson import ObjectId
            for key, val in query.items():
                if key == "$or":
                    if not any(self._match(doc, sub) for sub in val):
                        return False
                elif key == "$and":
                    if not all(self._match(doc, sub) for sub in val):
                        return False
                elif isinstance(val, dict):
                    doc_val = doc.get(key)
                    for op, op_val in val.items():
                        if op == "$lt" and not (doc_val is not None and doc_val < op_val):
                            return False
                        elif op == "$gte" and not (doc_val is not None and doc_val >= op_val):
                            return False
                elif "." in key:
                    # Nested field (e.g. members.user_id)
                    parts = key.split(".", 1)
                    sub_docs = doc.get(parts[0], [])
                    if isinstance(sub_docs, list):
                        if not any(self._match(s, {parts[1]: val}) for s in sub_docs):
                            return False
                    else:
                        if sub_docs != val:
                            return False
                else:
                    doc_val = doc.get(key)
                    if isinstance(doc_val, ObjectId):
                        doc_val = str(doc_val)
                    if isinstance(val, ObjectId):
                        val = str(val)
                    if doc_val != val:
                        return False
            return True

        def _apply_update(self, doc, update):
            if "$set" in update:
                doc.update(update["$set"])
            if "$push" in update:
                for field, val in update["$push"].items():
                    doc.setdefault(field, []).append(val)
            if "$pull" in update:
                for field, condition in update["$pull"].items():
                    doc[field] = [
                        item for item in doc.get(field, [])
                        if not self._match(item, condition)
                    ]

    class FakeCursor:
        def __init__(self, docs):
            self._docs = docs
            self._sort_key = None
            self._sort_dir = 1
            self._limit_n = None

        def sort(self, key, direction=1):
            self._sort_key = key
            self._sort_dir = direction
            return self

        def limit(self, n):
            self._limit_n = n
            return self

        async def to_list(self, _length=None):
            docs = list(self._docs)
            if self._sort_key:
                docs.sort(
                    key=lambda d: d.get(self._sort_key) or "",
                    reverse=(self._sort_dir == -1),
                )
            if self._limit_n:
                docs = docs[: self._limit_n]
            return docs

    class FakeDB:
        def __getattr__(self, name):
            if name not in collections:
                collections[name] = FakeCollection(name)
            return collections[name]

    return FakeDB()


@pytest_asyncio.fixture(scope="function")
async def client(mock_db):
    """Return an AsyncClient with the FastAPI test app, using the mock DB.

    We patch both _db (so get_db() returns our mock) and the lifecycle functions
    (connect_db / disconnect_db) so the real MongoDB is never touched.
    """
    with (
        patch.object(db_module, "_db", mock_db),
        patch.object(db_module, "connect_db", AsyncMock()),
        patch.object(db_module, "disconnect_db", AsyncMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
