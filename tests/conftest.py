"""Tests run against an in-memory MongoDB (mongomock) — nothing touches the real database.
The fake database must be in place before any part of the app is imported."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("RENDER", None)

from mongomock_motor import AsyncMongoMockClient  # noqa: E402

import app.db as dbmod  # noqa: E402

dbmod.db = AsyncMongoMockClient()["ashoka_distribution_test"]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.routers import auth as auth_router  # noqa: E402

db = dbmod.db


class Api:
    """Small helper: a signed-in admin with two companies, A (selected) and B."""

    def __init__(self, client):
        self.c = client
        r = client.post("/auth/bootstrap", json={"name": "Owner", "pin": "4321"})
        assert r.status_code == 200, r.text
        self.token = r.json()["access_token"]
        self.me = r.json()["user"]
        self.A = self.post("/companies", {"name": "Alpha", "kind": "distribution"}, company=None)["id"]
        self.B = self.post("/companies", {"name": "Beta", "kind": "distribution"}, company=None)["id"]

    def h(self, company="A"):
        h = {"Authorization": "Bearer " + self.token}
        if company:
            h["X-Company-Id"] = {"A": self.A, "B": self.B}.get(company, company)
        return h

    def get(self, path, company="A", **params):
        r = self.c.get(path, headers=self.h(company), params=params)
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
        return r.json()

    def post(self, path, body=None, company="A", ok=True):
        r = self.c.post(path, headers=self.h(company), json=body)
        if ok:
            assert r.status_code == 200, (path, r.status_code, r.text[:300])
            return r.json()
        return r

    def run(self, fn, *a):
        """Run a database call on the app's own event loop."""
        return self.c.portal.call(fn, *a)

    def insert(self, coll, docs):
        return self.run(db[coll].insert_many, docs)

    def find(self, coll, q=None):
        return self.run(lambda: db[coll].find(q or {}).to_list(None))


@pytest.fixture()
def api():
    with TestClient(app, raise_server_exceptions=False) as client:
        async def wipe():
            for n in await db.list_collection_names():
                await db[n].delete_many({})
        client.portal.call(wipe)
        auth_router._FAILS.clear()
        yield Api(client)
