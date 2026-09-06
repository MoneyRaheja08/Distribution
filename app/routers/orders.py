import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_user, is_staff, require_roles
from ..deps import current_company
from ..db import db
from ..models import ExecuteIn, OrderIn
from ..serializers import public_order

router = APIRouter(prefix="/orders", tags=["orders"])
staff_only = require_roles("admin", "manager")


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
