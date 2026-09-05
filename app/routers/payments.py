import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument

from ..auth import get_current_user, is_staff, require_roles
from ..db import db
from ..ledger import compute
from ..models import ApproveIn, ChequeUpdate, CollectIn, DepositIn
from ..serializers import public_payment

router = APIRouter(prefix="/payments", tags=["payments"])
staff_only = require_roles("admin", "manager")


async def _next_receipt() -> int:
    doc = await db.counters.find_one_and_update(
        {"_id": "receipt"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
    return 1000 + doc["seq"]


async def _summary(dealer_id):
    bills = [b async for b in db.bills.find({"dealer_id": dealer_id})]
    pays = [p async for p in db.payments.find({"dealer_id": dealer_id})]
    return compute(bills, pays)


@router.post("")
async def record_collection(body: CollectIn, user=Depends(get_current_user)):
    dealer = await db.dealers.find_one({"_id": body.dealer_id})
    if not dealer:
        raise HTTPException(404, "Dealer not found")
    if not is_staff(user) and dealer.get("collector_id") != user["_id"]:
        raise HTTPException(403, "Not your dealer")
    if user["role"] == "manager" and not user.get("can_collect", False):
        raise HTTPException(403, "You are not allowed to record collections")
    due = (await _summary(body.dealer_id))["outstanding"]
    if body.amount > due + 1:
        raise HTTPException(400, f"Amount exceeds outstanding of {due:.0f}")
    if body.mode.value == "Cheque" and not (body.cheque or "").strip():
        raise HTTPException(400, "Cheque number/bank is required for cheque payments")
    receipt = await _next_receipt()
    pending = body.mode.value == "Cheque"
    approved = user["role"] != "collector"   # collector payments need approval
    payment = {"_id": uuid.uuid4().hex, "dealer_id": dealer["_id"], "dealer_name": dealer["name"],
               "collector_id": user["_id"], "collector_name": user["name"], "amount": body.amount,
               "mode": body.mode.value, "cheque": (body.cheque or "").strip(), "date": date.today().isoformat(),
               "ts": datetime.now(timezone.utc), "receipt": receipt,
               "status": "pending" if pending else "cleared", "deposited": False, "approved": approved}
    await db.payments.insert_one(payment)
    out = public_payment(payment)
    out["new_outstanding"] = (await _summary(dealer["_id"]))["outstanding"]
    return out


@router.get("")
async def list_payments(dealer_id: Optional[str] = None, status: Optional[str] = None,
                        day: Optional[str] = None, user=Depends(get_current_user)):
    query = {}
    if not is_staff(user):
        query["collector_id"] = user["_id"]
    if dealer_id:
        query["dealer_id"] = dealer_id
    if status:
        query["status"] = status
    if day:
        query["date"] = day
    return [public_payment(p) async for p in db.payments.find(query).sort("ts", -1)]


@router.patch("/{pid}/cheque")
async def update_cheque(pid: str, body: ChequeUpdate, _=Depends(require_roles("admin"))):
    p = await db.payments.find_one({"_id": pid})
    if not p:
        raise HTTPException(404, "Payment not found")
    if p["mode"] != "Cheque" or p["status"] != "pending":
        raise HTTPException(400, "Only pending cheques can be updated")
    await db.payments.update_one({"_id": pid}, {"$set": {"status": "cleared" if body.cleared else "bounced"}})
    return {"ok": True}


@router.post("/deposit")
async def deposit_cash(body: DepositIn, _=Depends(require_roles("admin"))):
    r = await db.payments.update_many(
        {"collector_id": body.collector_id, "mode": "Cash", "deposited": False, "status": "cleared"},
        {"$set": {"deposited": True}})
    return {"ok": True, "marked": r.modified_count}


@router.get("/pending")
async def pending_approvals(_=Depends(staff_only)):
    return [public_payment(p) async for p in db.payments.find({"approved": False}).sort("ts", -1)]


@router.patch("/{pid}/approve")
async def approve_payment(pid: str, body: ApproveIn, _=Depends(staff_only)):
    p = await db.payments.find_one({"_id": pid})
    if not p:
        raise HTTPException(404, "Payment not found")
    if p.get("approved", True):
        raise HTTPException(400, "Already approved")
    if body.approved:
        await db.payments.update_one({"_id": pid}, {"$set": {"approved": True}})
    else:
        await db.payments.delete_one({"_id": pid})
    return {"ok": True}


@router.delete("/{pid}")
async def delete_payment(pid: str, _=Depends(require_roles("admin"))):
    r = await db.payments.delete_one({"_id": pid})
    if r.deleted_count == 0:
        raise HTTPException(404, "Payment not found")
    return {"ok": True}


@router.get("/summary")
async def dashboard(_=Depends(staff_only)):
    dealers = [d async for d in db.dealers.find()]
    bills_by, pays_by = {}, {}
    async for b in db.bills.find():
        bills_by.setdefault(b["dealer_id"], []).append(b)
    all_pays = [p async for p in db.payments.find()]
    for p in all_pays:
        pays_by.setdefault(p["dealer_id"], []).append(p)
    today = date.today().isoformat()
    total_out = over90 = 0
    for d in dealers:
        s = compute(bills_by.get(d["_id"], []), pays_by.get(d["_id"], []))
        total_out += s["outstanding"]
        over90 += s["ageing"].get("age_90p", 0)
    field_pays = [p for p in all_pays if p.get("collector_id") not in (None, "seed") and p.get("approved", True)]
    pending_count = sum(1 for p in all_pays if p.get("approved") is False)
    collected_today = sum(p["amount"] for p in field_pays if p["date"] == today and p["status"] != "bounced")
    cash_undeposited = sum(p["amount"] for p in field_pays
                           if p["mode"] == "Cash" and not p.get("deposited") and p["status"] == "cleared")
    cheques_pending = sum(p["amount"] for p in field_pays if p["status"] == "pending")
    users = [u async for u in db.users.find({"role": "collector"})]
    per_collector = []
    for u in users:
        got = sum(p["amount"] for p in field_pays
                  if p["collector_id"] == u["_id"] and p["date"] == today and p["status"] != "bounced")
        assigned = sum(1 for d in dealers if d.get("collector_id") == u["_id"])
        per_collector.append({"id": u["_id"], "name": u["name"], "dealers": assigned, "collected_today": got})
    return {"total_outstanding": total_out, "over_90_days": over90, "collected_today": collected_today,
            "cash_undeposited": cash_undeposited, "cheques_pending": cheques_pending,
            "pending_approvals": pending_count, "per_collector": per_collector}
