from datetime import date

from fastapi import APIRouter, Depends, Query

from ..auth import require_roles
from ..db import db
from ..ledger import compute

router = APIRouter(prefix="/reports", tags=["reports"])
admin = require_roles("admin")


def _live(p):
    return p.get("approved", True) and p.get("status") != "bounced" and p.get("collector_id") not in (None, "seed")


@router.get("/collections")
async def collections_report(frm: str = Query(alias="from"), to: str = Query(...), _=Depends(admin)):
    by_mode, by_collector, total, rows = {}, {}, 0, []
    async for p in db.payments.find({"date": {"$gte": frm, "$lte": to}}).sort("ts", -1):
        if not _live(p):
            continue
        amt = p["amount"]; total += amt
        by_mode[p["mode"]] = by_mode.get(p["mode"], 0) + amt
        c = by_collector.setdefault(p.get("collector_name") or "—", {"amount": 0, "count": 0})
        c["amount"] += amt; c["count"] += 1
        rows.append({"dealer": p.get("dealer_name"), "amount": round(amt), "mode": p["mode"],
                     "collector": p.get("collector_name"), "date": p.get("date")})
    return {"from": frm, "to": to, "total": round(total),
            "by_mode": {k: round(v) for k, v in by_mode.items()},
            "by_collector": [{"name": n, "amount": round(v["amount"]), "count": v["count"]}
                             for n, v in sorted(by_collector.items(), key=lambda x: -x[1]["amount"])],
            "rows": rows}


@router.get("/ageing")
async def ageing_report(_=Depends(admin)):
    dealers = [d async for d in db.dealers.find()]
    bills_by, pays_by = {}, {}
    async for b in db.bills.find():
        bills_by.setdefault(b["dealer_id"], []).append(b)
    async for p in db.payments.find():
        pays_by.setdefault(p["dealer_id"], []).append(p)
    ageing = {"age_0_30": 0, "age_31_60": 0, "age_61_90": 0, "age_90p": 0}
    total = 0; rows = []; over_limit = []
    for d in dealers:
        s = compute(bills_by.get(d["_id"], []), pays_by.get(d["_id"], []))
        total += s["outstanding"]
        for k in ageing:
            ageing[k] += s["ageing"].get(k, 0)
        if s["outstanding"] > 0:
            rows.append({"name": d["name"], "outstanding": s["outstanding"], "age_90p": s["ageing"].get("age_90p", 0)})
        lim = d.get("credit_limit", 0)
        if lim > 0 and s["outstanding"] > lim:
            over_limit.append({"name": d["name"], "outstanding": s["outstanding"], "limit": lim})
    top_overdue = sorted(rows, key=lambda r: (-r["age_90p"], -r["outstanding"]))[:15]
    dealer_ageing = []
    for d in dealers:
        s2 = compute(bills_by.get(d["_id"], []), pays_by.get(d["_id"], []))
        if s2["outstanding"] > 0:
            dealer_ageing.append({"name": d["name"], "outstanding": s2["outstanding"], **s2["ageing"]})
    dealer_ageing.sort(key=lambda r: -r["outstanding"])
    return {"total_outstanding": round(total), "ageing": ageing, "top_overdue": top_overdue,
            "over_limit": sorted(over_limit, key=lambda r: -r["outstanding"]), "dealers": dealer_ageing}


@router.get("/activity")
async def activity_report(frm: str = Query(alias="from"), to: str = Query(...), _=Depends(admin)):
    acc = {}
    async for p in db.payments.find({"date": {"$gte": frm, "$lte": to}}):
        if not _live(p):
            continue
        a = acc.setdefault(p.get("collector_name") or "—", {"collected": 0, "receipts": 0, "visits": 0, "dealers": set()})
        a["collected"] += p["amount"]; a["receipts"] += 1
    async for v in db.visits.find({"date": {"$gte": frm, "$lte": to}}):
        a = acc.setdefault(v.get("user_name") or "—", {"collected": 0, "receipts": 0, "visits": 0, "dealers": set()})
        a["visits"] += 1; a["dealers"].add(v.get("dealer_name"))
    return {"from": frm, "to": to,
            "rows": [{"name": n, "collected": round(a["collected"]), "receipts": a["receipts"],
                      "visits": a["visits"], "dealers_visited": len(a["dealers"])}
                     for n, a in sorted(acc.items(), key=lambda x: -x[1]["collected"])]}


@router.get("/sales-vs-collection")
async def sales_vs_collection(frm: str = Query(alias="from"), to: str = Query(...), _=Depends(admin)):
    dealers = {d["_id"]: d["name"] async for d in db.dealers.find()}
    acc = {}
    async for b in db.bills.find({"date": {"$gte": frm, "$lte": to}}):
        a = acc.setdefault(b["dealer_id"], {"sales": 0, "collected": 0})
        a["sales"] += b.get("amount", 0)
    async for p in db.payments.find({"date": {"$gte": frm, "$lte": to}}):
        if not _live(p):
            continue
        a = acc.setdefault(p["dealer_id"], {"sales": 0, "collected": 0})
        a["collected"] += p["amount"]
    rows = [{"name": dealers.get(did, "?"), "sales": round(v["sales"]), "collected": round(v["collected"]),
             "net": round(v["sales"] - v["collected"])} for did, v in acc.items()]
    rows.sort(key=lambda r: -r["sales"])
    tot_sales = sum(r["sales"] for r in rows)
    tot_coll = sum(r["collected"] for r in rows)
    return {"from": frm, "to": to, "rows": rows, "total_sales": tot_sales, "total_collected": tot_coll}
