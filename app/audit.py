"""Audit trail: who removed or overwrote what, with a copy of the removed records so nothing
is lost for good. Writing the trail must never block the action itself."""
import uuid
from datetime import datetime, timezone

from .db import db

MAX_DOCS = 3000      # per entry; a dealer with more lines than this keeps the first 3000


def _clip(docs):
    return list(docs or [])[:MAX_DOCS]


async def record(user, company, action, summary, removed=None):
    """removed = {collection_name: [documents]} exactly as they were before the change."""
    try:
        removed = {k: _clip(v) for k, v in (removed or {}).items() if v}
        await db.audit_log.insert_one({
            "_id": uuid.uuid4().hex, "ts": datetime.now(timezone.utc), "company_id": company,
            "user_id": (user or {}).get("_id"), "user_name": (user or {}).get("name"),
            "action": action, "summary": summary, "removed": removed,
            "counts": {k: len(v) for k, v in removed.items()},
        })
    except Exception as e:      # e.g. snapshot too large — keep a line without the copy
        try:
            await db.audit_log.insert_one({
                "_id": uuid.uuid4().hex, "ts": datetime.now(timezone.utc), "company_id": company,
                "user_id": (user or {}).get("_id"), "user_name": (user or {}).get("name"),
                "action": action, "summary": summary + " (copy not kept: " + type(e).__name__ + ")",
                "removed": {}, "counts": {}})
        except Exception:
            pass
