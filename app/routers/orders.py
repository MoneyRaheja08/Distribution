import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_user, is_staff, require_roles
from ..deps import current_company
from ..db import db
from ..models import ExecuteIn, OrderIn
from ..serializers import public_order
from ..ledger import sale_key

router = APIRouter(prefix="/orders", tags=["orders"])
staff_only = require_roles("admin", "manager")


def _norm(m):
    return (m or "").strip().lower()


@router.get("/dealer-models")
async def dealer_models(dealer_id: str, company=Depends(current_company), user=Depends(get_current_user)):
    """Models available in imported stock + how much of each model this dealer was already
    billed before (so the collector knows what the dealer already owes for that model)."""
    dealer = await db.dealers.find_one({"_id": dealer_id, "company_id": company})
    if not dealer:
        raise HTTPException(404, "Dealer not found")
    if not is_staff(user) and dealer.get("collector_id") != user["_id"]:
        raise HTTPException(403, "Not your dealer")

    # ---- stock models from imports (IMEI units + qty-tracked lots) ----
    agg = {}
    pipeline = [{"$match": {"company_id": company}},
                {"$group": {"_id": {"brand": "$brand", "model": "$model"},
                            "available": {"$sum": {"$cond": [{"$eq": ["$status", "in_stock"]}, 1, 0]}}}}]
    async for r in db.stock_units.aggregate(pipeline):
        m, b = r["_id"].get("model"), r["_id"].get("brand")
        if not m:
            continue
        agg[_norm(m)] = {"model": m, "brand": b, "available": r["available"]}
    async for l in db.stock_lots.find({"company_id": company}):
        m = l.get("model")
        if not m:
            continue
        av = (l.get("in_qty", 0) or 0) - (l.get("sold_qty", 0) or 0)
        k = _norm(m)
        if k in agg:
            agg[k]["available"] += av
        else:
            agg[k] = {"model": m, "brand": l.get("brand"), "available": av}
    stock = sorted(agg.values(), key=lambda x: ((x["brand"] or ""), (x["model"] or "")))

    # ---- this dealer's past billed history (dedupe re-imported duplicate lines) ----
    seen, billed = set(), {}
    async for s in db.sales.find({"company_id": company, "dealer_id": dealer_id}).sort("created_at", 1):
        k = sale_key(s)
        if k in seen:
            continue
        seen.add(k)
        nk = _norm(s.get("model"))
        if not nk:
            continue
        e = billed.setdefault(nk, {"model": s.get("model"), "brand": s.get("brand"), "units": 0, "value": 0.0})
        q = s.get("qty") or 0
        e["units"] += q if q > 0 else 1
        e["value"] += s.get("amount") or 0
    for e in billed.values():
        e["value"] = round(e["value"])
    return {"stock": stock, "billed": billed}


@router.get("")
async def list_orders(status: Optional[str] = None, company=Depends(current_company), user=Depends(get_current_user)):
    q = {"company_id": company}
    if not is_staff(user):
        q["created_by_id"] = user["_id"]     # collectors see their own orders
    if status:
        q["status"] = status
    return [public_order(o) async for o in db.orders.find(q).sort("ts", -1)]


@router.post("")
async def create_order(body: OrderIn, company=Depends(current_company), user=Depends(get_current_user)):
    dealer = await db.dealers.find_one({"_id": body.dealer_id, "company_id": company})
    if not dealer:
        raise HTTPException(404, "Dealer not found")
    if not is_staff(user) and dealer.get("collector_id") != user["_id"]:
        raise HTTPException(403, "Not your dealer")
    if not body.items:
        raise HTTPException(400, "Add at least one item")
    items = [i.model_dump() for i in body.items]
    total = sum((i["dp"] or 0) * i["qty"] for i in items)
    o = {"_id": uuid.uuid4().hex, "company_id": company, "dealer_id": dealer["_id"], "dealer_name": dealer["name"],
         "pricelist_name": body.pricelist_name or "", "note": body.note or "", "items": items, "total": round(total),
         "status": "pending", "date": date.today().isoformat(), "ts": datetime.now(timezone.utc),
         "created_by_id": user["_id"], "created_by_name": user["name"], "created_by_role": user["role"]}
    await db.orders.insert_one(o)
    return public_order(o)


@router.patch("/{oid}/execute")
async def execute_order(oid: str, body: ExecuteIn, company=Depends(current_company), user=Depends(staff_only)):
    o = await db.orders.find_one({"_id": oid, "company_id": company})
    if not o:
        raise HTTPException(404, "Order not found")
    if o.get("status") == "executed":
        raise HTTPException(400, "Order already executed")
    await db.orders.update_one({"_id": oid}, {"$set": {
        "status": "executed", "bill_no": (body.bill_no or "").strip(),
        "executed_by": user["name"], "executed_date": date.today().isoformat()}})
    return {"ok": True}


@router.delete("/{oid}")
async def delete_order(oid: str, company=Depends(current_company), _=Depends(staff_only)):
    await db.orders.delete_one({"_id": oid, "company_id": company})
    return {"ok": True}
