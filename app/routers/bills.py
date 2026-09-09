import uuid

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_user, require_roles
from ..deps import current_company
from ..db import db
from ..models import BulkBills

router = APIRouter(prefix="/bills", tags=["bills"])
staff_only = require_roles("admin", "manager")


@router.post("/bulk")
async def bulk_bills(body: BulkBills, company=Depends(current_company), _=Depends(require_roles("admin"))):
    """Add many bills at once. Rows match a dealer by id, or by name (case-insensitive)."""
    dealers = [d async for d in db.dealers.find({"company_id": company})]
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
                     "date": r.date, "amount": r.amount, "source": "bulk", "company_id": company})
        added += 1
    if docs:
        await db.bills.insert_many(docs)
    return {"ok": True, "added": added, "unmatched": unmatched, "duplicates": duplicates}


@router.get("/{bid}")
async def bill_detail(bid: str, company=Depends(current_company), _=Depends(get_current_user)):
    """Return a bill plus its imported line items (if it came from a Sale CSV import)."""
    b = await db.bills.find_one({"_id": bid, "company_id": company})
    if not b:
        raise HTTPException(404, "Bill not found")
    lines = []
    async for s in db.sales.find({"company_id": company, "dealer_id": b["dealer_id"], "bill_no": b.get("bill_no")}):
        lines.append({"model": s.get("model"), "group": s.get("group"), "brand": s.get("brand"),
                      "qty": s.get("qty"), "rate": s.get("rate"), "amount": s.get("amount"), "imei": s.get("imei")})
    return {"bill_no": b.get("bill_no"), "date": b.get("date"), "amount": b.get("amount"),
            "source": b.get("source"), "lines": lines,
            "line_total": round(sum(l["amount"] or 0 for l in lines), 2)}


@router.delete("/{bid}")
async def delete_bill(bid: str, company=Depends(current_company), _=Depends(require_roles("admin"))):
    """Admin: delete a bill (from PDF, import, statement or manual). Sale-CSV bills also
    return their sold stock to inventory and drop the linked sale lines."""
    b = await db.bills.find_one({"_id": bid, "company_id": company})
    if not b:
        raise HTTPException(404, "Bill not found")
    await db.bills.delete_one({"_id": bid, "company_id": company})
    reverted = 0
    if b.get("source") == "sale_csv" and b.get("bill_no"):
        async for u in db.stock_units.find({"company_id": company, "sale_bill": b["bill_no"],
                                            "sale_dealer_id": b["dealer_id"]}):
            await db.stock_units.update_one(
                {"_id": u["_id"]},
                {"$set": {"status": "in_stock"},
                 "$unset": {"sale_bill": "", "sale_dealer_id": "", "sale_dealer_name": "",
                            "sale_rate": "", "sale_date": ""}})
            reverted += 1
        await db.sales.delete_many({"company_id": company, "dealer_id": b["dealer_id"], "bill_no": b["bill_no"]})
    return {"ok": True, "reverted_units": reverted}
