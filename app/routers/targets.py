import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import get_current_user, is_staff, require_roles
from ..deps import current_company
from ..db import db
from ..ledger import _parse, brand_match

router = APIRouter(prefix="/targets", tags=["targets"])
admin_only = require_roles("admin")

OPENING_SRC = {"opening", "opening-bal", "openingbal"}
OPENING_NO = {"opening", "opening-bal", "opening balance"}


class TargetIn(BaseModel):
    dealer_id: str
    name: str
    target_value: float          # basic value (before GST)
    gst_pct: float = 18
    date_from: str
    date_to: str
    note: str = ""
    brand: str = ""              # blank = whole-ledger (overall); else this brand's sales only
    reward_type: str = ""        # "", "flat" (₹) or "pct" (% of achieved sales)
    reward_value: float = 0


class TargetPatch(BaseModel):
    name: Optional[str] = None
    target_value: Optional[float] = None
    gst_pct: Optional[float] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    note: Optional[str] = None
    brand: Optional[str] = None
    reward_type: Optional[str] = None
    reward_value: Optional[float] = None


def target_pub(t):
    t = dict(t)
    t["id"] = t.pop("_id")
    t.pop("company_id", None)
    return t


def _is_opening(b):
    return (b.get("source") or "").lower() in OPENING_SRC or (b.get("bill_no") or "").strip().lower() in OPENING_NO


def compute_target(t, gross, today=None):
    """Achievement from a precomputed gross (window-filtered). basic = gross stripped of GST."""
    today = today or date.today().isoformat()
    frm, to = t.get("date_from"), t.get("date_to")
    gst = t.get("gst_pct", 0) or 0
    basic = gross / (1 + gst / 100) if gst else gross
    tv = t.get("target_value", 0) or 0
    pct = round(basic / tv * 100, 1) if tv else 0.0
    remaining = max(0, round(tv - basic))
    d1, d2, dt = _parse(frm), _parse(to), _parse(today)
    total_days = max(1, (d2 - d1).days + 1) if d1 and d2 else 1
    elapsed = min(total_days, max(0, (dt - d1).days + 1)) if d1 and dt else 0
    elapsed_pct = round(elapsed / total_days * 100)
    days_left = max(0, (d2 - dt).days) if d2 and dt else 0
    expired = bool(d2 and dt and dt > d2)
    started = bool(d1 and dt and dt >= d1)
    done = tv > 0 and basic >= tv
    behind = started and (not done) and (not expired) and pct < (elapsed_pct - 5)
    status = "done" if done else ("expired" if expired else ("upcoming" if not started else ("behind" if behind else "on-track")))
    rt = t.get("reward_type") or ""
    rv = t.get("reward_value", 0) or 0
    reward_amount = round(rv) if rt == "flat" else (round(basic * rv / 100) if rt == "pct" else 0)
    reward_target = round(rv) if rt == "flat" else (round(tv * rv / 100) if rt == "pct" else 0)
    return {"gross_achieved": round(gross), "basic_achieved": round(basic), "achieved_pct": pct,
            "remaining": remaining, "elapsed_pct": elapsed_pct, "days_left": days_left,
            "total_days": total_days, "expired": expired, "done": done, "behind": behind,
            "started": started, "status": status, "brand": t.get("brand") or "",
            "reward_type": rt, "reward_value": rv, "reward_amount": reward_amount, "reward_target": reward_target}


def _gross_bills(bills, frm, to):
    g = 0.0
    for b in bills:
        d = b.get("date")
        if not d or d < frm or d > to or _is_opening(b):
            continue
        g += b.get("amount", 0) or 0
    return g


def _gross_sales(sales, frm, to, brand):
    g = 0.0
    for s in sales:
        d = s.get("date")
        if not d or d < frm or d > to:
            continue
        if brand and not brand_match(s, brand):
            continue
        g += s.get("amount", 0) or 0
    return g


def achievement(t, bills, sales, today=None):
    """Brand targets measure that brand's sales line-items; overall targets measure the whole ledger."""
    if t.get("brand"):
        gross = _gross_sales(sales, t.get("date_from"), t.get("date_to"), t["brand"])
    else:
        gross = _gross_bills(bills, t.get("date_from"), t.get("date_to"))
    return compute_target(t, gross, today)


@router.get("")
async def list_targets(company=Depends(current_company), user=Depends(get_current_user)):
    dealers = {d["_id"]: d async for d in db.dealers.find({"company_id": company})}
    staff = is_staff(user)
    allowed = set(dealers) if staff else {did for did, d in dealers.items() if d.get("collector_id") == user["_id"]}
    targets = [t async for t in db.dealer_targets.find({"company_id": company})]
    mine = [t for t in targets if t.get("dealer_id") in allowed]
    need_ids = list({t["dealer_id"] for t in mine})
    bills_by = {}
    if need_ids:
        async for b in db.bills.find({"company_id": company, "dealer_id": {"$in": need_ids}}):
            bills_by.setdefault(b["dealer_id"], []).append(b)
    names = list({(dealers.get(t["dealer_id"]) or {}).get("name") for t in mine if t.get("brand")} - {None})
    sales_by = {}
    if names:
        async for s in db.sales.find({"company_id": company, "dealer_name": {"$in": names}}):
            sales_by.setdefault(s.get("dealer_name"), []).append(s)
    today = date.today().isoformat()
    rows = []
    for t in mine:
        d = dealers.get(t["dealer_id"]) or {}
        rows.append({**target_pub(t), "dealer_name": d.get("name", "—"), "phone": d.get("phone"),
                     "collector_id": d.get("collector_id"),
                     **achievement(t, bills_by.get(t["dealer_id"], []), sales_by.get(d.get("name"), []), today)})
    rows.sort(key=lambda r: (0 if r["status"] == "behind" else 1, -(r["achieved_pct"] or 0), r["dealer_name"]))
    active = [r for r in rows if not r["expired"]]
    summary = {"targets": len(rows), "active": len(active),
               "behind": sum(1 for r in rows if r["behind"]),
               "done": sum(1 for r in rows if r["done"]),
               "on_track": sum(1 for r in active if r["status"] == "on-track"),
               "target_total": round(sum(r["target_value"] or 0 for r in active)),
               "achieved_total": round(sum(r["basic_achieved"] or 0 for r in active))}
    brands = sorted(b for b in await db.sales.distinct("brand", {"company_id": company}) if b)
    return {"rows": rows, "summary": summary, "is_staff": staff, "brands": brands}


@router.post("")
async def create_target(body: TargetIn, company=Depends(current_company), _=Depends(admin_only)):
    if not await db.dealers.find_one({"_id": body.dealer_id, "company_id": company}):
        raise HTTPException(404, "Dealer not found")
    if not (body.date_from and body.date_to and body.date_from <= body.date_to):
        raise HTTPException(400, "Give a valid start and end date")
    if not body.name.strip():
        raise HTTPException(400, "Give the target a name (e.g. Diwali 2026)")
    if not body.target_value or body.target_value <= 0:
        raise HTTPException(400, "Target value must be greater than 0")
    if body.reward_type not in ("", "flat", "pct"):
        raise HTTPException(400, "Reward type must be flat or pct")
    doc = {"_id": uuid.uuid4().hex, "company_id": company, "dealer_id": body.dealer_id,
           "name": body.name.strip(), "target_value": body.target_value, "gst_pct": body.gst_pct,
           "date_from": body.date_from, "date_to": body.date_to, "note": body.note.strip(),
           "brand": (body.brand or "").strip().upper(), "reward_type": body.reward_type,
           "reward_value": body.reward_value or 0, "created_at": datetime.now().isoformat()}
    await db.dealer_targets.insert_one(doc)
    return target_pub(doc)


@router.patch("/{tid}")
async def update_target(tid: str, body: TargetPatch, company=Depends(current_company), _=Depends(admin_only)):
    upd = {}
    for f in ("name", "target_value", "gst_pct", "date_from", "date_to", "note", "brand", "reward_type", "reward_value"):
        v = getattr(body, f)
        if v is not None:
            upd[f] = (v.strip().upper() if f == "brand" else v.strip()) if isinstance(v, str) else v
    if not upd:
        raise HTTPException(400, "Nothing to update")
    if ("date_from" in upd or "date_to" in upd):
        cur = await db.dealer_targets.find_one({"_id": tid, "company_id": company})
        if not cur:
            raise HTTPException(404, "Target not found")
        frm = upd.get("date_from", cur["date_from"]); to = upd.get("date_to", cur["date_to"])
        if not (frm and to and frm <= to):
            raise HTTPException(400, "Give a valid start and end date")
    r = await db.dealer_targets.update_one({"_id": tid, "company_id": company}, {"$set": upd})
    if not r.matched_count:
        raise HTTPException(404, "Target not found")
    return {"ok": True}


@router.delete("/{tid}")
async def delete_target(tid: str, company=Depends(current_company), _=Depends(admin_only)):
    r = await db.dealer_targets.delete_one({"_id": tid, "company_id": company})
    if not r.deleted_count:
        raise HTTPException(404, "Target not found")
    return {"ok": True}
