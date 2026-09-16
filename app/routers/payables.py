from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import get_current_user
from ..deps import current_company
from ..db import db

router = APIRouter(prefix="/payables", tags=["payables"])


async def payables_perm(user=Depends(get_current_user)):
    if user["role"] == "admin" or user.get("can_view_payments"):
        return user
    raise HTTPException(403, "You do not have access to company payments")


def _d(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


class BrandTermIn(BaseModel):
    brand: str
    credit_days: int = 0


class BillPatch(BaseModel):
    bill_no: str
    credit_days: Optional[int] = None
    paid: Optional[bool] = None
    paid_on: Optional[str] = None


@router.get("/brand-terms")
async def brand_terms(company=Depends(current_company), _=Depends(payables_perm)):
    terms = {}
    async for t in db.brand_terms.find({"company_id": company}):
        terms[t["brand"]] = t.get("credit_days", 0)
    brands = sorted({b for b in await db.purchases.distinct("brand", {"company_id": company}) if b})
    return {"brands": [{"brand": b, "credit_days": terms.get(b, 0)} for b in brands]}


@router.put("/brand-terms")
async def set_brand_term(body: BrandTermIn, company=Depends(current_company), _=Depends(payables_perm)):
    await db.brand_terms.update_one(
        {"company_id": company, "brand": body.brand},
        {"$set": {"credit_days": max(0, int(body.credit_days))}},
        upsert=True)
    return {"ok": True}


@router.get("")
async def payables(status: str = "outstanding", company=Depends(current_company), _=Depends(payables_perm)):
    terms = {}
    async for t in db.brand_terms.find({"company_id": company}):
        terms[t["brand"]] = t.get("credit_days", 0)
    ap = {}
    async for a in db.ap_bills.find({"company_id": company}):
        ap[a["bill_no"]] = a

    # aggregate purchase lines into bills (grouped by bill number)
    bills = {}
    async for p in db.purchases.find({"company_id": company}):
        bno = p.get("bill_no") or "(no bill no)"
        b = bills.setdefault(bno, {"bill_no": bno, "brand": None, "supplier": None,
                                   "date": None, "amount": 0.0, "lines": 0})
        b["amount"] += p.get("amount") or 0
        b["lines"] += 1
        b["brand"] = b["brand"] or p.get("brand")
        b["supplier"] = b["supplier"] or p.get("supplier")
        d = _d(p.get("date"))
        if d and (b["date"] is None or d < b["date"]):
            b["date"] = d

    today = date.today()
    out = []
    for bno, b in bills.items():
        a = ap.get(bno, {})
        cd = a.get("credit_days")
        override = cd is not None
        if cd is None:
            cd = terms.get(b["brand"], 0)
        due = (b["date"] + timedelta(days=int(cd))) if b["date"] else None
        days_left = (due - today).days if due else None
        paid = bool(a.get("paid"))
        out.append({
            "bill_no": bno, "brand": b["brand"] or "—", "supplier": b["supplier"] or "—",
            "date": b["date"].isoformat() if b["date"] else None,
            "amount": round(b["amount"]), "lines": b["lines"],
            "credit_days": int(cd), "credit_override": override,
            "due_date": due.isoformat() if due else None, "days_left": days_left,
            "overdue": (days_left is not None and days_left < 0 and not paid),
            "paid": paid, "paid_on": a.get("paid_on"),
        })

    if status == "outstanding":
        rows = [r for r in out if not r["paid"]]
    elif status == "paid":
        rows = [r for r in out if r["paid"]]
    elif status == "overdue":
        rows = [r for r in out if r["overdue"]]
    else:
        rows = out
    rows.sort(key=lambda r: (r["due_date"] or "9999-99-99", r["brand"]))

    summ = {}
    for r in out:
        if r["paid"]:
            continue
        s = summ.setdefault(r["brand"], {"brand": r["brand"], "amount": 0, "bills": 0,
                                         "overdue_amount": 0, "next_due": None})
        s["amount"] += r["amount"]
        s["bills"] += 1
        if r["overdue"]:
            s["overdue_amount"] += r["amount"]
        if r["due_date"] and (s["next_due"] is None or r["due_date"] < s["next_due"]):
            s["next_due"] = r["due_date"]
    summary = sorted(summ.values(), key=lambda x: -x["amount"])

    totals = {
        "outstanding": round(sum(r["amount"] for r in out if not r["paid"])),
        "overdue": round(sum(r["amount"] for r in out if r["overdue"])),
        "due_7": round(sum(r["amount"] for r in out if not r["paid"] and r["days_left"] is not None and 0 <= r["days_left"] <= 7)),
        "bills": len([r for r in out if not r["paid"]]),
    }
    return {"rows": rows, "summary": summary, "totals": totals}


@router.post("/bill")
async def patch_bill(body: BillPatch, company=Depends(current_company), _=Depends(payables_perm)):
    upd = {}
    if body.credit_days is not None:
        upd["credit_days"] = max(0, int(body.credit_days))
    if body.paid is not None:
        upd["paid"] = body.paid
        upd["paid_on"] = (body.paid_on or date.today().isoformat()) if body.paid else None
    elif body.paid_on is not None:
        upd["paid_on"] = body.paid_on
    if not upd:
        raise HTTPException(400, "Nothing to update")
    await db.ap_bills.update_one({"company_id": company, "bill_no": body.bill_no}, {"$set": upd}, upsert=True)
    return {"ok": True}
