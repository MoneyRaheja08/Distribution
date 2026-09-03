import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument

from ..auth import get_current_user, is_staff, require_roles
from ..db import db
from ..models import ChequeUpdate, CollectIn, DepositIn
from ..serializers import allocate, outstanding, public_payment, restore

router = APIRouter(prefix="/payments", tags=["payments"])
staff_only = require_roles("admin", "manager")


async def _next_receipt() -> int:
    doc = await db.counters.find_one_and_update(
        {"_id": "receipt"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return 1000 + doc["seq"]


@router.post("")
async def record_collection(body: CollectIn, user=Depends(get_current_user)):
    dealer = await db.dealers.find_one({"_id": body.dealer_id})
    if not dealer:
        raise HTTPException(404, "Dealer not found")
    # Collectors can only collect from their own dealers.
    if not is_staff(user) and dealer.get("collector_id") != user["_id"]:
        raise HTTPException(403, "Not your dealer")

    due = outstanding(dealer)
    if body.amount > due + 1e-6:
        raise HTTPException(400, f"Amount exceeds outstanding of {due:.0f}")
    if body.mode.value == "Cheque" and not (body.cheque or "").strip():
        raise HTTPException(400, "Cheque number/bank is required for cheque payments")

    ageing = dealer.get("ageing") or {}
    alloc = allocate(ageing, body.amount)
    await db.dealers.update_one({"_id": dealer["_id"]}, {"$set": {"ageing": ageing}})

    receipt = await _next_receipt()
    pending = body.mode.value == "Cheque"
    payment = {
        "_id": uuid.uuid4().hex,
        "dealer_id": dealer["_id"],
        "dealer_name": dealer["name"],
        "collector_id": user["_id"],
        "collector_name": user["name"],
        "amount": body.amount,
        "mode": body.mode.value,
        "cheque": (body.cheque or "").strip(),
        "date": date.today().isoformat(),
        "ts": datetime.now(timezone.utc),
        "receipt": receipt,
        "status": "pending" if pending else "cleared",
        "deposited": False,
        "alloc": alloc,
    }
    await db.payments.insert_one(payment)
    result = public_payment(payment)
    result["new_outstanding"] = outstanding({"ageing": ageing})
    return result


@router.get("")
async def list_payments(
    dealer_id: Optional[str] = None,
    status: Optional[str] = None,
    day: Optional[str] = None,  # YYYY-MM-DD
    user=Depends(get_current_user),
):
    query = {}
    if not is_staff(user):
        query["collector_id"] = user["_id"]
    if dealer_id:
        query["dealer_id"] = dealer_id
    if status:
        query["status"] = status
    if day:
        query["date"] = day
    items = [public_payment(p) async for p in db.payments.find(query).sort("ts", -1)]
    return items


@router.patch("/{pid}/cheque")
async def update_cheque(pid: str, body: ChequeUpdate, _=Depends(staff_only)):
    p = await db.payments.find_one({"_id": pid})
    if not p:
        raise HTTPException(404, "Payment not found")
    if p["mode"] != "Cheque" or p["status"] != "pending":
        raise HTTPException(400, "Only pending cheques can be updated")
    if body.cleared:
        await db.payments.update_one({"_id": pid}, {"$set": {"status": "cleared"}})
    else:
        # Bounced: restore the outstanding the payment had cleared.
        dealer = await db.dealers.find_one({"_id": p["dealer_id"]})
        if dealer:
            ageing = dealer.get("ageing") or {}
            restore(ageing, p.get("alloc"))
            await db.dealers.update_one({"_id": dealer["_id"]}, {"$set": {"ageing": ageing}})
        await db.payments.update_one({"_id": pid}, {"$set": {"status": "bounced"}})
    return {"ok": True}


@router.post("/deposit")
async def deposit_cash(body: DepositIn, _=Depends(staff_only)):
    """Mark a collector's undeposited cash as received at the counter."""
    r = await db.payments.update_many(
        {"collector_id": body.collector_id, "mode": "Cash", "deposited": False, "status": "cleared"},
        {"$set": {"deposited": True}},
    )
    return {"ok": True, "marked": r.modified_count}


@router.get("/summary")
async def dashboard(_=Depends(staff_only)):
    dealers = [d async for d in db.dealers.find()]
    payments = [p async for p in db.payments.find()]
    users = [u async for u in db.users.find({"role": "collector"})]
    today = date.today().isoformat()

    total_out = sum(outstanding(d) for d in dealers)
    over90 = sum(float((d.get("ageing") or {}).get("age_90p", 0) or 0) for d in dealers)
    collected_today = sum(p["amount"] for p in payments if p["date"] == today and p["status"] != "bounced")
    cash_undeposited = sum(
        p["amount"] for p in payments
        if p["mode"] == "Cash" and not p.get("deposited") and p["status"] == "cleared"
    )
    cheques_pending = sum(p["amount"] for p in payments if p["status"] == "pending")

    per_collector = []
    for u in users:
        got = sum(
            p["amount"] for p in payments
            if p["collector_id"] == u["_id"] and p["date"] == today and p["status"] != "bounced"
        )
        assigned = sum(1 for d in dealers if d.get("collector_id") == u["_id"])
        per_collector.append({"id": u["_id"], "name": u["name"], "dealers": assigned, "collected_today": got})

    return {
        "total_outstanding": total_out,
        "over_90_days": over90,
        "collected_today": collected_today,
        "cash_undeposited": cash_undeposited,
        "cheques_pending": cheques_pending,
        "per_collector": per_collector,
    }
