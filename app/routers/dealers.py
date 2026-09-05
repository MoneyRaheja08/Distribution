import re
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument

from ..auth import get_current_user, is_staff, require_roles
from ..db import db
from ..ledger import compute
from ..models import BillIn, BulkBills, DealerIn, DealerPatch, SeedIn
from ..serializers import public_dealer

router = APIRouter(prefix="/dealers", tags=["dealers"])
staff_only = require_roles("admin", "manager")


async def _summary(dealer_id):
    bills = [b async for b in db.bills.find({"dealer_id": dealer_id})]
    pays = [p async for p in db.payments.find({"dealer_id": dealer_id})]
    return compute(bills, pays)


@router.get("")
async def list_dealers(user=Depends(get_current_user)):
    query = {} if is_staff(user) else {"collector_id": user["_id"]}
    dealers = [d async for d in db.dealers.find(query)]
    ids = [d["_id"] for d in dealers]
    bills_by, pays_by = {}, {}
    async for b in db.bills.find({"dealer_id": {"$in": ids}}):
        bills_by.setdefault(b["dealer_id"], []).append(b)
    async for p in db.payments.find({"dealer_id": {"$in": ids}}):
        pays_by.setdefault(p["dealer_id"], []).append(p)
    today = date.today().isoformat()
    visited = set()
    async for v in db.visits.find({"user_id": user["_id"], "date": today}):
        visited.add(v["dealer_id"])
    return [public_dealer(d, compute(bills_by.get(d["_id"], []), pays_by.get(d["_id"], [])), d["_id"] in visited) for d in dealers]


@router.get("/{did}")
async def get_dealer(did: str, user=Depends(get_current_user)):
    d = await db.dealers.find_one({"_id": did})
    if not d:
        raise HTTPException(404, "Dealer not found")
    if not is_staff(user) and d.get("collector_id") != user["_id"]:
        raise HTTPException(403, "Not your dealer")
    today = date.today().isoformat()
    v = await db.visits.find_one({"user_id": user["_id"], "dealer_id": did, "date": today})
    return public_dealer(d, await _summary(did), bool(v))


@router.get("/{did}/ledger")
async def dealer_ledger(did: str, user=Depends(get_current_user)):
    d = await db.dealers.find_one({"_id": did})
    if not d:
        raise HTTPException(404, "Dealer not found")
    if not is_staff(user) and d.get("collector_id") != user["_id"]:
        raise HTTPException(403, "Not your dealer")
    bills = [b async for b in db.bills.find({"dealer_id": did})]
    pays = [p async for p in db.payments.find({"dealer_id": did})]
    rows = []
    for b in bills:
        rows.append({"date": b.get("date"), "type": "bill", "ref": b.get("bill_no"), "debit": b["amount"], "credit": 0})
    for p in pays:
        if p.get("status") == "bounced" or not p.get("approved", True):
            continue
        rows.append({"id": p["_id"], "date": p.get("date"), "type": "payment", "ref": p.get("cheque") or p.get("mode"),
                     "debit": 0, "credit": p["amount"], "mode": p.get("mode")})
    rows.sort(key=lambda r: (r["date"] or "0000-00-00"))
    bal = 0
    for r in rows:
        bal += r["debit"] - r["credit"]
        r["balance"] = round(bal)
    summ = compute(bills, pays)
    return {"dealer": d["name"], "outstanding": summ["outstanding"], "ageing": summ["ageing"],
            "last_payment": summ["last_payment"], "credit_limit": d.get("credit_limit", 0), "entries": rows}


@router.post("")
async def create_dealer(body: DealerIn, opening_balance: float = 0, _=Depends(staff_only)):
    did = uuid.uuid4().hex
    d = {"_id": did, "name": body.name.strip(), "area": body.area, "phone": body.phone,
         "credit_limit": body.credit_limit, "collector_id": body.collector_id}
    await db.dealers.insert_one(d)
    if opening_balance and opening_balance > 0:
        await db.bills.insert_one({"_id": uuid.uuid4().hex, "dealer_id": did, "bill_no": "Opening",
                                   "date": date.today().isoformat(), "amount": opening_balance, "source": "opening"})
    return public_dealer(d, await _summary(did))


@router.patch("/{did}")
async def update_dealer(did: str, body: DealerPatch, _=Depends(staff_only)):
    upd = {}
    for f in ("name", "area", "phone", "credit_limit", "collector_id"):
        v = getattr(body, f)
        if v is not None:
            upd[f] = v.strip() if f == "name" else v
    if not upd:
        raise HTTPException(400, "Nothing to update")
    d = await db.dealers.find_one_and_update({"_id": did}, {"$set": upd}, return_document=ReturnDocument.AFTER)
    if not d:
        raise HTTPException(404, "Dealer not found")
    return public_dealer(d, await _summary(did))


@router.delete("/{did}")
async def delete_dealer(did: str, _=Depends(require_roles("admin"))):
    await db.bills.delete_many({"dealer_id": did})
    await db.payments.delete_many({"dealer_id": did})
    await db.dealers.delete_one({"_id": did})
    return {"ok": True}


@router.post("/{did}/bills")
async def add_bill(did: str, body: BillIn, _=Depends(staff_only)):
    if not await db.dealers.find_one({"_id": did}):
        raise HTTPException(404, "Dealer not found")
    dupe = await db.bills.find_one({"dealer_id": did,
                                    "bill_no": {"$regex": f"^{re.escape(body.bill_no.strip())}$", "$options": "i"}})
    if dupe:
        raise HTTPException(409, f"Bill {body.bill_no.strip()} already exists for this dealer")
    await db.bills.insert_one({"_id": uuid.uuid4().hex, "dealer_id": did, "bill_no": body.bill_no.strip(),
                               "date": body.date, "amount": body.amount, "source": "manual"})
    return {"ok": True, "summary": await _summary(did)}


@router.post("/{did}/seed")
async def seed_ledger(did: str, body: SeedIn, _=Depends(require_roles("admin"))):
    """Replace a dealer's whole ledger from an imported statement (one-time seed)."""
    if not await db.dealers.find_one({"_id": did}):
        raise HTTPException(404, "Dealer not found")
    await db.bills.delete_many({"dealer_id": did})
    await db.payments.delete_many({"dealer_id": did})
    docs = []
    if body.opening and body.opening > 0:
        docs.append({"_id": uuid.uuid4().hex, "dealer_id": did, "bill_no": "Opening",
                     "date": body.opening_date or "2000-01-01", "amount": body.opening, "source": "opening"})
    for b in body.bills:
        docs.append({"_id": uuid.uuid4().hex, "dealer_id": did, "bill_no": b.bill_no,
                     "date": b.date, "amount": b.amount, "source": "statement"})
    if docs:
        await db.bills.insert_many(docs)
    pdocs = []
    for p in body.payments:
        pdocs.append({"_id": uuid.uuid4().hex, "dealer_id": did, "collector_id": "seed", "collector_name": "Statement",
                      "amount": p.amount, "mode": "Cheque", "cheque": p.ref or "", "date": p.date,
                      "receipt": None, "status": "cleared", "deposited": True})
    if pdocs:
        await db.payments.insert_many(pdocs)
    return {"ok": True, "summary": await _summary(did)}


@router.post("/{did}/visit")
async def mark_visited(did: str, user=Depends(get_current_user)):
    d = await db.dealers.find_one({"_id": did})
    if not d:
        raise HTTPException(404, "Dealer not found")
    if not is_staff(user) and d.get("collector_id") != user["_id"]:
        raise HTTPException(403, "Not your dealer")
    today = date.today().isoformat()
    now = datetime.now(timezone.utc)
    await db.visits.update_one(
        {"dealer_id": did, "user_id": user["_id"], "date": today},
        {"$setOnInsert": {"_id": uuid.uuid4().hex, "dealer_id": did, "dealer_name": d["name"],
                          "user_id": user["_id"], "user_name": user["name"], "role": user["role"],
                          "date": today, "first_ts": now},
         "$set": {"last_ts": now}},
        upsert=True)
    return {"ok": True}
