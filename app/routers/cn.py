import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_roles
from ..deps import current_company
from ..db import db
from ..ledger import brand_match_any
from ..models import CnComputeIn, CnRuleIn

router = APIRouter(prefix="/cn", tags=["cn"])
staff_only = require_roles("admin")   # CN calculator is admin-only


async def _rate_qty(company, frm, to, brands):
    """Per model over a period: billed qty, latest rate (the period's price), and whether
    the rate varied within the period."""
    out = {}
    q = {"company_id": company, "date": {"$gte": frm, "$lte": to}}
    async for p in db.purchases.find(q).sort("date", 1):
        if brands and not brand_match_any(p, brands):
            continue
        model = (p.get("model") or "—")
        qty = p.get("qty", 0) or 0
        amt = p.get("amount", 0) or 0
        rate = p.get("rate") or (amt / qty if qty else 0)
        e = out.setdefault(model, {"model": model, "brand": p.get("brand"),
                                   "group": p.get("group") or p.get("sub_group") or "",
                                   "qty": 0, "amount": 0.0, "rate": None, "rate_date": None, "rates": set()})
        e["qty"] += qty
        e["amount"] += amt
        if rate:
            e["rates"].add(round(rate))
        d = p.get("date") or ""
        if e["rate_date"] is None or d >= e["rate_date"]:
            e["rate"] = round(rate)
            e["rate_date"] = d
    return out


@router.post("/compute")
async def compute_cn(body: CnComputeIn, company=Depends(current_company), _=Depends(staff_only)):
    """Credit-note amount = (current price − base price) × units billed in the current period,
    per model. Base/current prices come from each model's latest purchase rate in each period,
    unless overridden manually."""
    base = await _rate_qty(company, body.base_from, body.base_to, body.brand)
    cur = await _rate_qty(company, body.cur_from, body.cur_to, body.brand)
    ov = {}
    for o in body.overrides:
        ov[(o.model or "").strip()] = o

    rows = []
    total_cn = 0.0
    by_brand = {}
    for model, c in cur.items():
        o = ov.get(model.strip())
        b = base.get(model)
        old_rate = o.old_price if (o and o.old_price is not None) else (b["rate"] if b else None)
        new_rate = o.new_price if (o and o.new_price is not None) else c["rate"]
        units = c["qty"]
        diff = (new_rate - old_rate) if (old_rate is not None and new_rate is not None) else None
        cn = round(diff * units) if diff is not None else 0
        total_cn += cn
        if diff is not None:
            bb = by_brand.setdefault(c["brand"] or "—", 0)
            by_brand[c["brand"] or "—"] = bb + cn
        rows.append({
            "model": model, "brand": c["brand"], "group": c["group"],
            "old_rate": old_rate, "new_rate": new_rate, "units": units,
            "diff": round(diff) if diff is not None else None, "cn": cn,
            "missing_base": old_rate is None,
            "overridden": bool(o),
            "base_varied": bool(b and len(b["rates"]) > 1),
            "cur_varied": len(c["rates"]) > 1,
        })
    rows.sort(key=lambda r: -(r["cn"] or 0))
    return {
        "base_from": body.base_from, "base_to": body.base_to,
        "cur_from": body.cur_from, "cur_to": body.cur_to,
        "total_cn": round(total_cn),
        "by_brand": [{"brand": k, "cn": round(v)} for k, v in sorted(by_brand.items(), key=lambda x: -x[1])],
        "rows": rows,
        "missing_count": sum(1 for r in rows if r["missing_base"]),
        "model_count": len(rows),
    }


def _public_rule(r):
    return {"id": r["_id"], "name": r.get("name"), "brand": r.get("brand") or [],
            "base_from": r.get("base_from"), "base_to": r.get("base_to"),
            "cur_from": r.get("cur_from"), "cur_to": r.get("cur_to"),
            "overrides": r.get("overrides") or []}


@router.get("/rules")
async def list_rules(company=Depends(current_company), _=Depends(staff_only)):
    rules = [r async for r in db.cn_rules.find({"company_id": company}).sort("name", 1)]
    return {"rules": [_public_rule(r) for r in rules]}


@router.post("/rules")
async def save_rule(body: CnRuleIn, company=Depends(current_company), _=Depends(staff_only)):
    if not body.name.strip():
        raise HTTPException(400, "Rule set name is required")
    doc = {"name": body.name.strip(), "brand": body.brand,
           "base_from": body.base_from, "base_to": body.base_to,
           "cur_from": body.cur_from, "cur_to": body.cur_to,
           "overrides": [o.model_dump() for o in body.overrides], "company_id": company}
    if body.id:
        await db.cn_rules.update_one({"_id": body.id, "company_id": company}, {"$set": doc})
        rid = body.id
    else:
        rid = uuid.uuid4().hex
        doc["_id"] = rid
        doc["created_at"] = datetime.now(timezone.utc)
        await db.cn_rules.insert_one(doc)
    return {"ok": True, "id": rid}


@router.delete("/rules/{rid}")
async def delete_rule(rid: str, company=Depends(current_company), _=Depends(staff_only)):
    await db.cn_rules.delete_one({"_id": rid, "company_id": company})
    return {"ok": True}
