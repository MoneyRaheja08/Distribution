from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query

from fastapi import HTTPException
from ..auth import get_current_user, require_roles
from ..deps import current_company
from ..db import db
from ..ledger import compute, bill_breakdown

router = APIRouter(prefix="/reports", tags=["reports"])


async def admin(user=Depends(get_current_user)):
    if user["role"] == "admin" or user.get("can_view_reports"):
        return user
    raise HTTPException(403, "You do not have reports access")


def _live(p):
    return p.get("approved", True) and p.get("status") != "bounced" and p.get("collector_id") not in (None, "seed")


@router.get("/collections")
async def collections_report(frm: str = Query(alias="from"), to: str = Query(...), company=Depends(current_company), _=Depends(admin)):
    by_mode, by_collector, total, rows = {}, {}, 0, []
    async for p in db.payments.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}).sort("ts", -1):
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
async def ageing_report(company=Depends(current_company), _=Depends(admin)):
    dealers = [d async for d in db.dealers.find({"company_id": company})]
    bills_by, pays_by = {}, {}
    async for b in db.bills.find({"company_id": company}):
        bills_by.setdefault(b["dealer_id"], []).append(b)
    async for p in db.payments.find({"company_id": company}):
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
async def activity_report(frm: str = Query(alias="from"), to: str = Query(...), company=Depends(current_company), _=Depends(admin)):
    acc = {}
    async for p in db.payments.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
        if not _live(p):
            continue
        a = acc.setdefault(p.get("collector_name") or "—", {"collected": 0, "receipts": 0, "visits": 0, "dealers": set()})
        a["collected"] += p["amount"]; a["receipts"] += 1
    async for v in db.visits.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
        a = acc.setdefault(v.get("user_name") or "—", {"collected": 0, "receipts": 0, "visits": 0, "dealers": set()})
        a["visits"] += 1; a["dealers"].add(v.get("dealer_name"))
    return {"from": frm, "to": to,
            "rows": [{"name": n, "collected": round(a["collected"]), "receipts": a["receipts"],
                      "visits": a["visits"], "dealers_visited": len(a["dealers"])}
                     for n, a in sorted(acc.items(), key=lambda x: -x[1]["collected"])]}


@router.get("/sales-vs-collection")
async def sales_vs_collection(frm: str = Query(alias="from"), to: str = Query(...), company=Depends(current_company), _=Depends(admin)):
    dealers = {d["_id"]: d["name"] async for d in db.dealers.find({"company_id": company})}
    acc = {}
    async for b in db.bills.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
        a = acc.setdefault(b["dealer_id"], {"sales": 0, "collected": 0})
        a["sales"] += b.get("amount", 0)
    async for p in db.payments.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
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


@router.get("/bill-ageing")
async def bill_ageing(company=Depends(current_company), _=Depends(admin)):
    dealers = [d async for d in db.dealers.find({"company_id": company})]
    bills_by, pays_by = {}, {}
    async for b in db.bills.find({"company_id": company}):
        bills_by.setdefault(b["dealer_id"], []).append(b)
    async for p in db.payments.find({"company_id": company}):
        pays_by.setdefault(p["dealer_id"], []).append(p)
    result = []
    for d in dealers:
        bd = bill_breakdown(bills_by.get(d["_id"], []), pays_by.get(d["_id"], []))
        if bd:
            result.append({"name": d["name"], "outstanding": sum(x["unpaid"] for x in bd), "bills": bd})
    result.sort(key=lambda r: -r["outstanding"])
    return {"dealers": result}


@router.get("/bills")
async def bills_report(frm: str = Query(alias="from"), to: str = Query(...), source: Optional[str] = None,
                       company=Depends(current_company), _=Depends(admin)):
    dealers = {d["_id"]: d["name"] async for d in db.dealers.find({"company_id": company})}
    q = {"company_id": company, "date": {"$gte": frm, "$lte": to}}
    if source:
        q["source"] = source
    rows, total = [], 0
    async for b in db.bills.find(q).sort("date", -1):
        amt = b.get("amount", 0); total += amt
        rows.append({"dealer": dealers.get(b["dealer_id"], "?"), "bill_no": b.get("bill_no"),
                     "date": b.get("date"), "amount": round(amt), "source": b.get("source")})
    return {"from": frm, "to": to, "source": source, "total": round(total), "rows": rows}


@router.get("/sales")
async def sales_report(frm: str = Query(alias="from"), to: str = Query(...), q: str = "", brand: str = "",
                       company=Depends(current_company), _=Depends(admin)):
    import re as _re
    query = {"company_id": company, "date": {"$gte": frm, "$lte": to}}
    if brand:
        query["brand"] = brand.upper()
    rx = _re.compile(_re.escape(q), _re.I) if q else None
    rows, total, units, by_dealer, by_model = [], 0.0, 0, {}, {}
    async for s in db.sales.find(query).sort("date", -1):
        if rx and not (rx.search(s.get("model") or "") or rx.search(s.get("dealer_name") or "") or rx.search(s.get("imei") or "")):
            continue
        amt = s.get("amount", 0); total += amt
        if s.get("imei"):
            units += 1
        rows.append({"date": s.get("date"), "bill_no": s.get("bill_no"), "dealer": s.get("dealer_name"),
                     "brand": s.get("brand"), "group": s.get("group"), "model": s.get("model"),
                     "imei": s.get("imei"), "qty": s.get("qty", 0), "rate": round(s.get("rate", 0)), "amount": round(amt)})
        d = by_dealer.setdefault(s.get("dealer_name") or "—", {"amount": 0, "qty": 0})
        d["amount"] += amt; d["qty"] += s.get("qty", 0) or 1
        m = by_model.setdefault(s.get("model") or "—", {"model": s.get("model") or "—", "brand": s.get("brand"), "amount": 0, "qty": 0})
        m["amount"] += amt; m["qty"] += s.get("qty", 0) or 1
    return {"from": frm, "to": to, "total": round(total), "units": units, "count": len(rows), "rows": rows[:1000],
            "by_dealer": [{"dealer": k, "amount": round(v["amount"]), "qty": v["qty"]}
                          for k, v in sorted(by_dealer.items(), key=lambda x: -x[1]["amount"])],
            "by_model": [{"model": v["model"], "brand": v["brand"], "amount": round(v["amount"]), "qty": v["qty"]}
                         for v in sorted(by_model.values(), key=lambda x: -x["qty"])][:20]}


@router.get("/purchases-brand")
async def purchases_brand_report(frm: str = Query(alias="from"), to: str = Query(...), brand: str = "",
                                 company=Depends(current_company), _=Depends(admin)):
    query = {"company_id": company, "date": {"$gte": frm, "$lte": to}}
    if brand:
        query["brand"] = brand.upper()
    total, by_brand, by_month, by_cat = 0.0, {}, {}, {}
    async for p in db.purchases.find(query):
        amt = p.get("amount", 0); total += amt
        qty = p.get("qty", 0) or 1
        b = by_brand.setdefault(p.get("brand") or "—", {"amount": 0, "qty": 0}); b["amount"] += amt; b["qty"] += qty
        mm = by_month.setdefault((p.get("date") or "")[:7] or "—", {"amount": 0, "qty": 0}); mm["amount"] += amt; mm["qty"] += qty
        cc = by_cat.setdefault(p.get("group") or p.get("sub_group") or "Other", {"amount": 0, "qty": 0}); cc["amount"] += amt; cc["qty"] += qty
    return {"from": frm, "to": to, "total": round(total),
            "by_brand": [{"brand": k, "amount": round(v["amount"]), "qty": v["qty"]} for k, v in sorted(by_brand.items(), key=lambda x: -x[1]["amount"])],
            "by_month": [{"month": k, "amount": round(v["amount"]), "qty": v["qty"]} for k, v in sorted(by_month.items())],
            "by_category": [{"group": k, "amount": round(v["amount"]), "qty": v["qty"]} for k, v in sorted(by_cat.items(), key=lambda x: -x[1]["amount"])]}


@router.get("/profit")
async def profit_report(frm: str = Query(alias="from"), to: str = Query(...), brand: str = "",
                        company=Depends(current_company), _=Depends(admin)):
    query = {"company_id": company, "status": "sold", "sale_date": {"$gte": frm, "$lte": to}}
    if brand:
        query["brand"] = brand.upper()
    by_model, tot_sale, tot_cost, units = {}, 0.0, 0.0, 0
    by_month = {}
    async for u in db.stock_units.find(query):
        sr = u.get("sale_rate") or 0
        pr = u.get("purchase_rate") or 0
        units += 1; tot_sale += sr; tot_cost += pr
        m = by_model.setdefault(u.get("model") or "—", {"model": u.get("model") or "—", "brand": u.get("brand"), "qty": 0, "sale": 0, "cost": 0})
        m["qty"] += 1; m["sale"] += sr; m["cost"] += pr
        mo = (u.get("sale_date") or "")[:7] or "—"
        mm = by_month.setdefault(mo, {"sale": 0, "cost": 0})
        mm["sale"] += sr; mm["cost"] += pr
    rows = [{"model": v["model"], "brand": v["brand"], "qty": v["qty"], "sale": round(v["sale"]),
             "cost": round(v["cost"]), "margin": round(v["sale"] - v["cost"])} for v in by_model.values()]
    rows.sort(key=lambda x: -x["margin"])
    return {"from": frm, "to": to, "units": units, "total_sale": round(tot_sale), "total_cost": round(tot_cost),
            "total_margin": round(tot_sale - tot_cost), "rows": rows,
            "by_month": [{"month": k, "sale": round(v["sale"]), "cost": round(v["cost"]), "margin": round(v["sale"] - v["cost"])}
                         for k, v in sorted(by_month.items())]}


@router.get("/brand-scorecard")
async def brand_scorecard(frm: str = Query(alias="from"), to: str = Query(...),
                          company=Depends(current_company), _=Depends(admin)):
    brands = {}

    def b(name):
        return brands.setdefault(name or "—", {"brand": name or "—", "purchase_amount": 0, "purchase_qty": 0,
                                               "sale_amount": 0, "sale_units": 0, "stock_value": 0, "stock_units": 0, "margin": 0})
    async for p in db.purchases.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
        x = b(p.get("brand")); x["purchase_amount"] += p.get("amount", 0); x["purchase_qty"] += p.get("qty", 0) or 1
    async for s in db.sales.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
        x = b(s.get("brand")); x["sale_amount"] += s.get("amount", 0); x["sale_units"] += 1 if s.get("imei") else (s.get("qty", 0) or 1)
    async for u in db.stock_units.find({"company_id": company, "status": "in_stock"}):
        x = b(u.get("brand")); x["stock_value"] += u.get("purchase_rate") or 0; x["stock_units"] += 1
    async for u in db.stock_units.find({"company_id": company, "status": "sold", "sale_date": {"$gte": frm, "$lte": to}}):
        x = b(u.get("brand")); x["margin"] += (u.get("sale_rate") or 0) - (u.get("purchase_rate") or 0)
    rows = [{k: (round(v[k]) if k != "brand" else v[k]) for k in v} for v in brands.values()]
    rows.sort(key=lambda r: -r["sale_amount"])
    return {"from": frm, "to": to, "rows": rows}
