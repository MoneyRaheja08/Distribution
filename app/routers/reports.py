from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query

from fastapi import HTTPException
from ..auth import get_current_user
from ..deps import current_company
from ..db import db
from ..ledger import compute, bill_breakdown, brand_match, brand_query, sale_key

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
                       dealer: str = "", model: str = "",
                       company=Depends(current_company), _=Depends(admin)):
    import re as _re
    query = {"company_id": company, "date": {"$gte": frm, "$lte": to}}
    rx = _re.compile(_re.escape(q), _re.I) if q else None
    bsel, dsel, msel = brand.strip().upper(), dealer.strip(), model.strip()
    rows, total, units, by_dealer, by_model = [], 0.0, 0, {}, {}
    all_dealers, all_models, all_brands = set(), set(), set()
    seen, dups = {}, 0
    async for s in db.sales.find(query).sort([("date", -1), ("created_at", 1)]):
        k = sale_key(s); bt = s.get("import_batch") or ""
        if k in seen and seen[k] != bt:
            dups += 1
            continue
        seen[k] = bt
        if s.get("dealer_name"): all_dealers.add(s["dealer_name"])
        if s.get("model"): all_models.add(s["model"])
        if s.get("brand"): all_brands.add(s["brand"])
        if bsel and not brand_match(s, bsel):
            continue
        if dsel and (s.get("dealer_name") or "") != dsel:
            continue
        if msel and (s.get("model") or "") != msel:
            continue
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
    return {"from": frm, "to": to, "total": round(total), "units": units, "count": len(rows), "rows": rows[:2000], "duplicates_ignored": dups,
            "dealers": sorted(all_dealers), "models": sorted(all_models), "brands": sorted(all_brands),
            "by_dealer": [{"dealer": k, "amount": round(v["amount"]), "qty": v["qty"]}
                          for k, v in sorted(by_dealer.items(), key=lambda x: -x[1]["amount"])],
            "by_model": [{"model": v["model"], "brand": v["brand"], "amount": round(v["amount"]), "qty": v["qty"]}
                         for v in sorted(by_model.values(), key=lambda x: -x["qty"])]}


@router.get("/purchases-brand")
async def purchases_brand_report(frm: str = Query(alias="from"), to: str = Query(...), brand: str = "",
                                 company=Depends(current_company), _=Depends(admin)):
    query = {"company_id": company, "date": {"$gte": frm, "$lte": to}}
    if brand:
        query.update(brand_query(brand))
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
        query.update(brand_query(brand))
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
    brands = sorted(b for b in await db.stock_units.distinct("brand", {"company_id": company}) if b)
    return {"from": frm, "to": to, "brand": brand.upper(), "brands": brands,
            "units": units, "total_sale": round(tot_sale), "total_cost": round(tot_cost),
            "total_margin": round(tot_sale - tot_cost), "rows": rows,
            "by_month": [{"month": k, "sale": round(v["sale"]), "cost": round(v["cost"]), "margin": round(v["sale"] - v["cost"])}
                         for k, v in sorted(by_month.items())]}


@router.get("/profit2")
async def profit2_report(frm: str = Query(alias="from"), to: str = Query(...), brand: str = "",
                         company=Depends(current_company), _=Depends(admin)):
    """Real working-capital & return model, per brand. Revenue, COGS, gross margin and
    stock value come from the imported Sale/Purchase catalog for the chosen brand.
    Receivables aren't stored per brand, so when a brand is picked they are shown as an
    estimated share (by that brand's revenue). Scheme/opex/cost-of-capital/Haier-credit
    are set on the client."""
    from datetime import date as _date
    try:
        y1, m1, d1 = [int(x) for x in frm.split("-")]; y2, m2, d2 = [int(x) for x in to.split("-")]
        days = max(1, (_date(y2, m2, d2) - _date(y1, m1, d1)).days + 1)
    except Exception:
        days = 30
    factor = 365.0 / days
    bsel = brand.strip().upper()

    # avg purchase cost per (brand, model) + collect brand list
    model_cost, brands_seen = {}, set()
    async for pr in db.purchases.find({"company_id": company}):
        bb = (pr.get("brand") or "—")
        brands_seen.add(bb)
        mc = model_cost.setdefault((bb, pr.get("model") or "—"), {"amt": 0.0, "qty": 0})
        mc["amt"] += pr.get("amount", 0) or 0; mc["qty"] += (pr.get("qty", 0) or 0)

    def avg_cost(bb, model):
        mc = model_cost.get((bb, model))
        return (mc["amt"] / mc["qty"]) if mc and mc["qty"] else 0.0

    imei_pr = {}
    async for u in db.stock_units.find({"company_id": company, "status": "sold"}, {"imei": 1, "purchase_rate": 1}):
        if u.get("imei"):
            imei_pr[u["imei"]] = u.get("purchase_rate") or 0

    revenue = cogs = total_rev_all = 0.0
    units = dups = 0
    seen, by_model, by_month = {}, {}, {}
    dq = {"no_date": 0, "no_date_amount": 0.0, "other_brand": 0, "other_brand_amount": 0.0, "no_brand": 0, "no_brand_amount": 0.0,
          "no_cost_units": 0, "no_cost_amount": 0.0, "unmatched_dealer": 0, "unmatched_dealer_amount": 0.0,
          "stock_units_no_cost": 0, "total_lines_all": 0}
    async for sdoc in db.sales.find({"company_id": company, "$or": [{"date": None}, {"date": ""}]}):
        dq["no_date"] += 1; dq["no_date_amount"] += sdoc.get("amount", 0) or 0
    dq["stock_units_no_cost"] = await db.stock_units.count_documents({"company_id": company, "status": "in_stock", "$or": [{"purchase_rate": None}, {"purchase_rate": 0}]})
    async for sdoc in db.sales.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}).sort("created_at", 1):
        amt = sdoc.get("amount", 0) or 0
        bb = (sdoc.get("brand") or "—").strip().upper()
        model = sdoc.get("model") or "—"
        brands_seen.add(bb)
        dq["total_lines_all"] += 1
        # same line re-imported in another batch → count once
        key = (sdoc.get("bill_no"), sdoc.get("imei")) if sdoc.get("imei") else (sdoc.get("bill_no"), model, sdoc.get("qty"), amt)
        batch = sdoc.get("import_batch") or ""
        if key in seen and seen[key] != batch:
            dups += 1
            continue
        seen[key] = batch
        total_rev_all += amt
        if bsel and not brand_match(sdoc, bsel):
            if bb == "—":
                dq["no_brand"] += 1; dq["no_brand_amount"] += amt
            else:
                dq["other_brand"] += 1; dq["other_brand_amount"] += amt
            continue
        revenue += amt
        if sdoc.get("imei"):
            q = 1
            c = imei_pr.get(sdoc["imei"], avg_cost(bb, model))
        else:
            q = sdoc.get("qty", 0) or 0
            c = q * avg_cost(bb, model)
        if c <= 0 and amt > 0:
            dq["no_cost_units"] += q; dq["no_cost_amount"] += amt
        if not sdoc.get("dealer_id"):
            dq["unmatched_dealer"] += 1; dq["unmatched_dealer_amount"] += amt
        units += q; cogs += c
        m = by_model.setdefault(model, {"model": model, "brand": bb, "qty": 0, "sale": 0.0, "cost": 0.0})
        m["qty"] += q; m["sale"] += amt; m["cost"] += c
        mo = (sdoc.get("date") or "")[:7] or "—"
        mm = by_month.setdefault(mo, {"sale": 0.0, "cost": 0.0})
        mm["sale"] += amt; mm["cost"] += c
    gross = revenue - cogs
    rows = [{"model": v["model"], "brand": v["brand"], "qty": v["qty"], "sale": round(v["sale"]), "cost": round(v["cost"]),
             "margin": round(v["sale"] - v["cost"]),
             "margin_pct": round((v["sale"] - v["cost"]) / v["sale"] * 100, 1) if v["sale"] else 0} for v in by_model.values()]
    rows.sort(key=lambda x: -x["sale"])

    # stock value on hand (brand-filtered)
    stock_value = 0.0
    su_q = {"company_id": company, "status": "in_stock"}
    if bsel:
        su_q.update(brand_query(bsel))
    async for u in db.stock_units.find(su_q, {"purchase_rate": 1}):
        stock_value += u.get("purchase_rate") or 0
    lot_q = {"company_id": company}
    if bsel:
        lot_q.update(brand_query(bsel))
    async for lot in db.stock_lots.find(lot_q):
        avail = (lot.get("in_qty", 0) or 0) - (lot.get("sold_qty", 0) or 0)
        if avail > 0:
            stock_value += avail * avg_cost(lot.get("brand") or "—", lot.get("model") or "—")

    # receivables (company-wide), prorated to the brand by its revenue share
    dealers = [d["_id"] async for d in db.dealers.find({"company_id": company}, {"_id": 1})]
    bills_by, pays_by = {}, {}
    async for b in db.bills.find({"company_id": company}):
        bills_by.setdefault(b["dealer_id"], []).append(b)
    async for pmt in db.payments.find({"company_id": company}):
        pays_by.setdefault(pmt["dealer_id"], []).append(pmt)
    total_receivables = 0.0
    for did in dealers:
        total_receivables += compute(bills_by.get(did, []), pays_by.get(did, []))["outstanding"]
    if bsel and total_rev_all > 0:
        receivables = total_receivables * (revenue / total_rev_all)
        recv_est = True
    else:
        receivables = total_receivables
        recv_est = False

    ann_sales = revenue * factor
    ann_cogs = cogs * factor
    stock_days = (stock_value / ann_cogs * 365) if ann_cogs > 0 else 0
    recv_days = (receivables / ann_sales * 365) if ann_sales > 0 else 0
    return {"from": frm, "to": to, "period_days": days,
            "brand": bsel, "brands": sorted(b for b in brands_seen if b and b != "—"),
            "revenue": round(revenue), "cogs": round(cogs), "gross": round(gross),
            "gross_margin_pct": round(gross / revenue * 100, 2) if revenue else 0,
            "units": units, "duplicates_ignored": dups, "rows": rows,
            "data_check": {k: (round(v) if isinstance(v, float) else v) for k, v in dq.items()},
            "by_month": [{"month": k, "sale": round(v["sale"]), "cost": round(v["cost"]), "margin": round(v["sale"] - v["cost"])}
                         for k, v in sorted(by_month.items())],
            "stock_value": round(stock_value),
            "receivables": round(receivables), "receivables_estimated": recv_est,
            "total_receivables": round(total_receivables),
            "ann_sales": round(ann_sales), "ann_cogs": round(ann_cogs),
            "stock_days": round(stock_days), "recv_days": round(recv_days)}


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


@router.get("/top-performers")
async def top_performers(frm: str = Query(alias="from"), to: str = Query(...),
                         company=Depends(current_company), _=Depends(admin)):
    skus, deal = {}, {}
    async for s in db.sales.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
        amt = s.get("amount", 0); qty = s.get("qty", 0) or 1
        k = s.get("model") or "—"
        x = skus.setdefault(k, {"model": k, "brand": s.get("brand"), "amount": 0, "qty": 0}); x["amount"] += amt; x["qty"] += qty
        dn = s.get("dealer_name") or "—"
        y = deal.setdefault(dn, {"dealer": dn, "amount": 0, "qty": 0}); y["amount"] += amt; y["qty"] += qty
    top_skus = sorted([{**v, "amount": round(v["amount"])} for v in skus.values()], key=lambda x: -x["amount"])[:10]
    top_dealers = sorted([{**v, "amount": round(v["amount"])} for v in deal.values()], key=lambda x: -x["amount"])[:10]
    return {"from": frm, "to": to, "top_skus": top_skus, "top_dealers": top_dealers}


@router.get("/beat")
async def beat_report(frm: str = Query(alias="from"), to: str = Query(...),
                      company=Depends(current_company), _=Depends(admin)):
    """Collector accountability: per collector, assigned dealers vs visited, amount collected, cheques taken."""
    # assigned dealers per collector
    assigned = {}
    dealer_name = {}
    async for d in db.dealers.find({"company_id": company}):
        dealer_name[d["_id"]] = d.get("name")
        if d.get("collector_id"):
            assigned[d["collector_id"]] = assigned.get(d["collector_id"], 0) + 1
    # collectors
    who = {}
    async for u in db.users.find({"role": "collector"}):
        if company in (u.get("company_ids") or []):
            who[u["_id"]] = u.get("name")
    # visits in range (distinct dealers per collector)
    visits = {}
    async for v in db.visits.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
        uid = v.get("user_id")
        visits.setdefault(uid, {"name": v.get("user_name"), "dealers": set(), "count": 0})
        visits[uid]["dealers"].add(v.get("dealer_id"))
        visits[uid]["count"] += 1
    # collections in range (approved, non-bounced) per collector
    coll = {}
    async for p in db.payments.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
        if p.get("approved", True) is False or p.get("status") == "bounced":
            continue
        cid = p.get("collector_id")
        if not cid or cid == "seed":
            continue
        c = coll.setdefault(cid, {"amount": 0, "receipts": 0, "cheques": 0, "cheque_amt": 0})
        c["amount"] += p.get("amount", 0) or 0
        c["receipts"] += 1
        if (p.get("mode") or "") == "Cheque":
            c["cheques"] += 1
            c["cheque_amt"] += p.get("amount", 0) or 0
    ids = set(who) | set(visits) | set(coll)
    rows = []
    for uid in ids:
        name = who.get(uid) or (visits.get(uid, {}) or {}).get("name") or "—"
        v = visits.get(uid, {})
        c = coll.get(uid, {})
        rows.append({
            "collector": name,
            "assigned": assigned.get(uid, 0),
            "visited": len(v.get("dealers", set())) if v else 0,
            "visits": v.get("count", 0) if v else 0,
            "collected": round(c.get("amount", 0)),
            "receipts": c.get("receipts", 0),
            "cheques": c.get("cheques", 0),
            "cheque_amt": round(c.get("cheque_amt", 0)),
        })
    rows.sort(key=lambda r: -r["collected"])
    tot = {"collected": sum(r["collected"] for r in rows), "receipts": sum(r["receipts"] for r in rows),
           "visited": sum(r["visited"] for r in rows), "assigned": sum(r["assigned"] for r in rows),
           "cheques": sum(r["cheques"] for r in rows)}
    return {"from": frm, "to": to, "rows": rows, "totals": tot}


@router.get("/followup")
async def followup_report(min_days: int = 0, min_amount: float = 0, bucket: str = "",
                          company=Depends(current_company), _=Depends(admin)):
    """Who to chase today: dealers with outstanding, sorted by amount, with phone, oldest-due days,
    last payment, and a per-bucket breakdown for a ready action list."""
    dealers = [d async for d in db.dealers.find({"company_id": company})]
    bills_by, pays_by = {}, {}
    async for b in db.bills.find({"company_id": company}):
        bills_by.setdefault(b["dealer_id"], []).append(b)
    async for p in db.payments.find({"company_id": company}):
        pays_by.setdefault(p["dealer_id"], []).append(p)
    rows = []
    for d in dealers:
        bl = bills_by.get(d["_id"], []); pz = pays_by.get(d["_id"], [])
        s = compute(bl, pz)
        if s["outstanding"] <= 0:
            continue
        oldest = max((u["days"] for u in bill_breakdown(bl, pz)), default=0)
        ag = s["ageing"]
        if bucket and (ag.get(bucket, 0) or 0) <= 0:
            continue
        if oldest < min_days or s["outstanding"] < min_amount:
            continue
        rows.append({
            "dealer": d["name"], "dealer_id": d["_id"], "area": d.get("area"), "phone": d.get("phone"),
            "outstanding": s["outstanding"], "oldest_due": oldest,
            "age_0_30": ag.get("age_0_30", 0), "age_31_60": ag.get("age_31_60", 0),
            "age_61_90": ag.get("age_61_90", 0), "age_90p": ag.get("age_90p", 0),
            "over_limit": bool(d.get("credit_limit") and s["outstanding"] > d.get("credit_limit", 0)),
            "last_payment": s.get("last_payment"),
        })
    rows.sort(key=lambda r: (-r["oldest_due"], -r["outstanding"]))
    return {"count": len(rows), "total": round(sum(r["outstanding"] for r in rows)), "rows": rows}
