import uuid
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import get_current_user
from ..deps import current_company
from ..db import db
from ..costlib import cost_override_map  # noqa: F401 (re-exported for callers)
from .reports import profit_perm

router = APIRouter(prefix="/cost-overrides", tags=["costs"])


async def admin_only(user=Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    return user


class OverrideIn(BaseModel):
    brand: str = ""
    model: str
    nlc: float


class BulkIn(BaseModel):
    items: List[OverrideIn]


def _pub(o):
    return {"id": o["_id"], "brand": o.get("brand") or "", "model": o.get("model") or "",
            "nlc": o.get("nlc", 0), "updated_at": o.get("updated_at"), "updated_by": o.get("updated_by")}


@router.get("")
async def list_overrides(company=Depends(current_company), _=Depends(profit_perm)):
    rows = [_pub(o) async for o in db.cost_overrides.find({"company_id": company}).sort("model", 1)]
    return {"rows": rows}


async def _upsert(company, user, brand, model, nlc):
    b = (brand or "").strip().upper()
    m = (model or "").strip()
    if not m or nlc is None or nlc < 0:
        raise HTTPException(400, "Model and a valid NLC are required")
    await db.cost_overrides.update_one(
        {"company_id": company, "brand": b, "model": m},
        {"$set": {"nlc": round(float(nlc), 2), "updated_at": datetime.now().isoformat(), "updated_by": user.get("name")},
         "$setOnInsert": {"_id": uuid.uuid4().hex}},
        upsert=True)


@router.post("")
async def set_override(body: OverrideIn, company=Depends(current_company), user=Depends(admin_only)):
    await _upsert(company, user, body.brand, body.model, body.nlc)
    return {"ok": True}


@router.post("/bulk")
async def set_bulk(body: BulkIn, company=Depends(current_company), user=Depends(admin_only)):
    n = 0
    for it in body.items:
        if (it.model or "").strip() and it.nlc and it.nlc > 0:
            await _upsert(company, user, it.brand, it.model, it.nlc)
            n += 1
    return {"ok": True, "saved": n}


@router.delete("/{oid}")
async def del_override(oid: str, company=Depends(current_company), user=Depends(admin_only)):
    r = await db.cost_overrides.delete_one({"_id": oid, "company_id": company})
    if not r.deleted_count:
        raise HTTPException(404, "Not found")
    return {"ok": True}
