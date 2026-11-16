"""Two read-only helpers for the screens:
GET /insights/trend    month-by-month billed, collected and outstanding (for charts)
GET /insights/search   find a dealer or a bill from anywhere in the app
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import get_current_user, is_staff
from ..db import db
from ..deps import current_company
from ..loaders import ledger_maps
from ..timeutil import today_ist

router = APIRouter(prefix="/insights", tags=["insights"])


def _live(p):
    """Same rule as the ledger: a bounced or not-yet-approved payment does not count."""
    return p.get("status") != "bounced" and p.get("approved", True)


async def trend_access(user=Depends(get_current_user)):
    if user["role"] == "admin" or user.get("can_view_reports") or user.get("can_view_dashboard"):
        return user
    raise HTTPException(403, "You do not have reports access")


@router.get("/trend")
async def trend(months: int = 12, company=Depends(current_company), _=Depends(trend_access)):
    """For each of the last N months: what was billed, what was collected, and what dealers
    owed at the end of that month (the current month runs up to today).

    Outstanding at a month end = for each dealer, bills dated up to then minus payments dated
    up to then, never below zero (an advance from one dealer does not reduce what others owe).
    The last point therefore equals today's total outstanding."""
    months = max(2, min(int(months), 36))
    today = today_ist()
    y, m = today.year, today.month
    keys = []
    for _i in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    first = keys[0]
    idx = {k: i for i, k in enumerate(keys)}
    billed = [0.0] * months
    collected = [0.0] * months
    outstanding = [0.0] * months

    bills_by, pays_by = await ledger_maps(company)
    for did in set(bills_by) | set(pays_by):
        delta = [0.0] * months          # net change in this dealer's balance per month
        opening = 0.0                   # balance before the first month shown
        for b in bills_by.get(did, []):
            k, amt = (b.get("date") or "")[:7], float(b.get("amount") or 0)
            if k < first:
                opening += amt
            elif k in idx:
                delta[idx[k]] += amt
                billed[idx[k]] += amt
        for p in pays_by.get(did, []):
            if not _live(p):
                continue
            k, amt = (p.get("date") or "")[:7], float(p.get("amount") or 0)
            if k < first:
                opening -= amt
            elif k in idx:
                delta[idx[k]] -= amt
                collected[idx[k]] += amt
        bal = opening
        for i in range(months):
            bal += delta[i]
            if bal > 0.5:
                outstanding[i] += bal

    rows = [{"month": k, "billed": round(billed[i]), "collected": round(collected[i]),
             "outstanding": round(outstanding[i])} for i, k in enumerate(keys)]
    return {"months": months, "as_of": today.isoformat(), "rows": rows}


@router.get("/search")
async def search(q: str = Query(""), company=Depends(current_company), user=Depends(get_current_user)):
    """Dealers by name, area or phone; bills by bill number. A collector only finds their own dealers."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"dealers": [], "bills": []}
    rx = {"$regex": re.escape(q), "$options": "i"}
    scope = {"company_id": company}
    if not is_staff(user):
        scope["collector_id"] = user["_id"]

    conds = [{"name": rx}, {"area": rx}]
    digits = re.sub(r"\D", "", q)
    if len(digits) >= 3:
        conds.append({"phone": {"$regex": re.escape(digits)}})
    dealers = await db.dealers.find({**scope, "$or": conds}).limit(40).to_list(None)
    ql = q.lower()
    dealers.sort(key=lambda d: (not (d.get("name") or "").lower().startswith(ql), (d.get("name") or "").lower()))
    dealers = dealers[:8]

    bill_q = {"company_id": company, "bill_no": rx}
    names = {}
    if not is_staff(user):
        mine = await db.dealers.find(scope, {"name": 1}).to_list(None)
        names = {d["_id"]: d.get("name") for d in mine}
        bill_q["dealer_id"] = {"$in": list(names)}
    bills = await db.bills.find(bill_q).sort("date", -1).limit(8).to_list(None)
    missing = [b["dealer_id"] for b in bills if b.get("dealer_id") not in names]
    if missing:
        async for d in db.dealers.find({"_id": {"$in": missing}}, {"name": 1}):
            names[d["_id"]] = d.get("name")
    return {
        "dealers": [{"id": d["_id"], "name": d.get("name"), "area": d.get("area"), "phone": d.get("phone")} for d in dealers],
        "bills": [{"id": b["_id"], "bill_no": b.get("bill_no"), "date": b.get("date"), "amount": b.get("amount"),
                   "dealer_id": b.get("dealer_id"), "dealer": names.get(b.get("dealer_id")) or "—"} for b in bills],
    }
