import uuid

from fastapi import APIRouter, Depends

from ..auth import require_roles
from ..db import db
from ..models import BulkBills

router = APIRouter(prefix="/bills", tags=["bills"])
staff_only = require_roles("admin", "manager")


@router.post("/bulk")
async def bulk_bills(body: BulkBills, _=Depends(staff_only)):
    """Add many bills at once. Rows match a dealer by id, or by name (case-insensitive)."""
    dealers = [d async for d in db.dealers.find()]
    by_id = {d["_id"]: d for d in dealers}
    by_name = {d["name"].strip().lower(): d for d in dealers}
    existing = set()
    async for b in db.bills.find({}, {"dealer_id": 1, "bill_no": 1}):
        existing.add((b["dealer_id"], (b.get("bill_no") or "").strip().lower()))
    added, unmatched, duplicates = 0, [], []
    docs = []
    for r in body.bills:
        d = by_id.get(r.dealer_id) if r.dealer_id else None
        if not d and r.dealer_name:
            d = by_name.get(r.dealer_name.strip().lower())
        if not d:
            unmatched.append(r.dealer_name or r.dealer_id or "?")
            continue
        key = (d["_id"], (r.bill_no or "").strip().lower())
        if key in existing:
            duplicates.append(r.bill_no)
            continue
        existing.add(key)
        docs.append({"_id": uuid.uuid4().hex, "dealer_id": d["_id"], "bill_no": r.bill_no,
                     "date": r.date, "amount": r.amount, "source": "bulk"})
        added += 1
    if docs:
        await db.bills.insert_many(docs)
    return {"ok": True, "added": added, "unmatched": unmatched, "duplicates": duplicates}
