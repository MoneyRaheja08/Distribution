import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..auth import get_current_user
from ..deps import current_company
from ..db import db
from ..ledger import compute, bill_breakdown, _parse, brand_match
from .reports import admin

router = APIRouter(prefix="/reports", tags=["reports"])


def _live(p):
    return p.get("approved", True) and p.get("status") != "bounced"


async def _ledger_ctx(company):
    dealers = {d["_id"]: d async for d in db.dealers.find({"company_id": company})}
    bills_by, pays_by = {}, {}
    async for b in db.bills.find({"company_id": company}):
        bills_by.setdefault(b["dealer_id"], []).append(b)
    async for p in db.payments.find({"company_id": company}):
        if _live(p):
            pays_by.setdefault(p["dealer_id"], []).append(p)
    return dealers, bills_by, pays_by


def _fifo(bills, payments, today=None):
    """Per bill: paid_date (when FIFO payments fully covered it) and delay days."""
    today = today or date.today()
    bs = sorted(bills, key=lambda b: b.get("date") or "9999")
    ps = sorted(payments, key=lambda p: p.get("date") or "9999")
    pi, avail = 0, 0.0
    pay_date = None
    out = []
    for b in bs:
        need = float(b.get("amount") or 0)
        while need > 0.5 and (avail > 0.5 or pi < len(ps)):
            if avail <= 0.5:
                avail = float(ps[pi].get("amount") or 0); pay_date = ps[pi].get("date"); pi += 1
            take = min(avail, need); avail -= take; need -= take
        bd = _parse(b.get("date"))
        if need <= 0.5 and bd:
            pd = _parse(pay_date) or today
            out.append({"bill": b, "paid": True, "delay": max(0, (pd - bd).days), "unpaid": 0.0})
        else:
            out.append({"bill": b, "paid": False, "delay": (today - bd).days if bd else 0, "unpaid": need})
    return out


async def _cost_ctx(company):
    model_cost = {}
    async for pr in db.purchases.find({"company_id": company}):
        mc = model_cost.setdefault((pr.get("brand") or "—", pr.get("model") or "—"), {"amt": 0.0, "qty": 0})
        mc["amt"] += pr.get("amount", 0) or 0; mc["qty"] += pr.get("qty", 0) or 0
    imei_pr = {}
    async for u in db.stock_units.find({"company_id": company}, {"imei": 1, "purchase_rate": 1}):
        if u.get("imei") and u.get("purchase_rate"):
            imei_pr[u["imei"]] = u["purchase_rate"]

    def cost_of(s):
        if s.get("imei") and s["imei"] in imei_pr:
            return imei_pr[s["imei"]]
        mc = model_cost.get((s.get("brand") or "—", s.get("model") or "—"))
        q = 1 if s.get("imei") else (s.get("qty", 0) or 0)
        return (mc["amt"] / mc["qty"] * q) if mc and mc["qty"] else 0.0
    return cost_of


async def _sales(company, frm, to, brand=""):
    q = {"company_id": company, "date": {"$gte": frm, "$lte": to}}
    seen, out = set(), []
    async for s in db.sales.find(q).sort("created_at", 1):
        if brand and not brand_match(s, brand):
            continue
        key = (s.get("bill_no"), s.get("imei")) if s.get("imei") else (s.get("bill_no"), s.get("model"), s.get("qty"), s.get("amount"))
        if key in seen:
            continue
        seen.add(key); out.append(s)
    return out


# 4. Dealer scorecard 2.0 -------------------------------------------------------------
@router.get("/dealer-scorecard")
async def dealer_scorecard(frm: str = Query(alias="from"), to: str = Query(...),
                           company=Depends(current_company), _=Depends(admin)):
    dealers, bills_by, pays_by = await _ledger_ctx(company)
    cost_of = await _cost_ctx(company)
    by_name = {d["name"].strip().lower(): did for did, d in dealers.items() if d.get("name")}
    margin, revenue_cat = {}, {}
    for s in await _sales(company, frm, to):
        did = s.get("dealer_id") or by_name.get((s.get("dealer_name") or "").strip().lower())
        if not did:
            continue
        m = margin.setdefault(did, {"sale": 0.0, "cost": 0.0, "units": 0})
        m["sale"] += s.get("amount", 0) or 0; m["cost"] += cost_of(s); m["units"] += 1 if s.get("imei") else (s.get("qty") or 0)
    rows = []
    for did, d in dealers.items():
        bl, pz = bills_by.get(did, []), pays_by.get(did, [])
        sales = sum(float(b.get("amount") or 0) for b in bl if frm <= (b.get("date") or "") <= to)
        coll = sum(float(p.get("amount") or 0) for p in pz if frm <= (p.get("date") or "") <= to)
        if not bl and not pz:
            continue
        led = compute(bl, pz)
        f = _fifo(bl, pz)
        paid = [x for x in f if x["paid"]]
        wsum = sum(float(x["bill"].get("amount") or 0) for x in paid)
        avg_delay = round(sum(x["delay"] * float(x["bill"].get("amount") or 0) for x in paid) / wsum) if wsum else None
        oldest_unpaid = max([x["delay"] for x in f if not x["paid"]], default=0)
        m = margin.get(did, {"sale": 0.0, "cost": 0.0, "units": 0})
        gm = m["sale"] - m["cost"]
        rows.append({"id": did, "name": d.get("name"), "area": d.get("area"), "phone": d.get("phone"),
                     "sales": round(sales), "collections": round(coll), "outstanding": led["outstanding"],
                     "avg_delay": avg_delay, "oldest_unpaid": oldest_unpaid, "units": m["units"],
                     "margin": round(gm), "margin_pct": round(gm / m["sale"] * 100, 1) if m["sale"] else 0,
                     "age_90p": led["ageing"]["age_90p"], "credit_limit": d.get("credit_limit") or 0})
    if not rows:
        return {"from": frm, "to": to, "rows": [], "totals": {}}
    sales_sorted = sorted((r["sales"] for r in rows if r["sales"] > 0), reverse=True)
    big_cut = sales_sorted[max(0, len(sales_sorted) // 3 - 1)] if sales_sorted else 0
    mp = sorted(r["margin_pct"] for r in rows if r["sales"] > 0)
    med_mp = mp[len(mp) // 2] if mp else 0
    for r in rows:
        delay = r["avg_delay"] if r["avg_delay"] is not None else r["oldest_unpaid"]
        fast = delay <= 30
        slow = delay > 45 or r["age_90p"] > 0
        big = r["sales"] >= big_cut and r["sales"] > 0
        good_m = r["margin_pct"] >= med_mp and r["margin"] > 0
        if r["sales"] == 0 and r["outstanding"] == 0:
            tag = "inactive"
        elif good_m and fast:
            tag = "star"
        elif big and slow:
            tag = "big_slow"
        elif slow:
            tag = "slow"
        elif fast:
            tag = "fast"
        else:
            tag = "ok"
        r["tag"] = tag
        r["fin_cost"] = round(r["outstanding"] * 0.12 / 365 * max(delay, 1))
        r["true_margin"] = r["margin"] - r["fin_cost"]
    rows.sort(key=lambda r: -r["true_margin"])
    tot = {"sales": sum(r["sales"] for r in rows), "collections": sum(r["collections"] for r in rows),
           "outstanding": sum(r["outstanding"] for r in rows), "margin": sum(r["margin"] for r in rows),
           "dealers": len(rows), "stars": sum(1 for r in rows if r["tag"] == "star"),
           "big_slow": sum(1 for r in rows if r["tag"] == "big_slow")}
    return {"from": frm, "to": to, "rows": rows, "totals": tot, "median_margin_pct": med_mp}


# 5. Dealer credit-days trend ------------------------------------------------------------
@router.get("/credit-trend")
async def credit_trend(months: int = 6, company=Depends(current_company), _=Depends(admin)):
    dealers, bills_by, pays_by = await _ledger_ctx(company)
    today = date.today()
    mlist = []
    y, m = today.year, today.month
    for _i in range(months):
        mlist.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12; y -= 1
    mlist.reverse()
    rows = []
    for did, d in dealers.items():
        bl, pz = bills_by.get(did, []), pays_by.get(did, [])
        if not bl:
            continue
        f = _fifo(bl, pz, today)
        per = {mo: {"w": 0.0, "d": 0.0, "n": 0, "open": 0} for mo in mlist}
        for x in f:
            mo = (x["bill"].get("date") or "")[:7]
            if mo not in per:
                continue
            amt = float(x["bill"].get("amount") or 0)
            per[mo]["w"] += amt; per[mo]["d"] += x["delay"] * amt; per[mo]["n"] += 1
            if not x["paid"]:
                per[mo]["open"] += 1
        series = [{"month": mo, "delay": round(v["d"] / v["w"]) if v["w"] else None, "bills": v["n"], "open": v["open"]} for mo, v in per.items()]
        vals = [s["delay"] for s in series if s["delay"] is not None]
        if len(vals) < 2:
            continue
        first_half = vals[:max(1, len(vals) // 2)]; second_half = vals[len(vals) // 2:]
        a = sum(first_half) / len(first_half); b = sum(second_half) / len(second_half)
        change = round(b - a)
        rising = len(vals) >= 3 and vals[-1] > vals[-2] > vals[-3]
        flag = "warning" if (change >= 15 or rising) else ("improving" if change <= -10 else "steady")
        rows.append({"id": did, "name": d.get("name"), "phone": d.get("phone"), "series": series, "change": change,
                     "latest": vals[-1], "flag": flag, "outstanding": compute(bl, pz)["outstanding"]})
    order = {"warning": 0, "steady": 1, "improving": 2}
    rows.sort(key=lambda r: (order[r["flag"]], -r["outstanding"]))
    return {"months": mlist, "rows": rows, "warnings": sum(1 for r in rows if r["flag"] == "warning")}


# 6. Lost / inactive dealers ----------------------------------------------------------------
@router.get("/inactive-dealers")
async def inactive_dealers(days: int = 30, company=Depends(current_company), _=Depends(admin)):
    dealers, bills_by, pays_by = await _ledger_ctx(company)
    today = date.today()
    rows = []
    for did, d in dealers.items():
        bl = bills_by.get(did, [])
        dates = sorted(_parse(b.get("date")) for b in bl if _parse(b.get("date")))
        if not dates:
            continue
        last = dates[-1]
        since = (today - last).days
        if since < days:
            continue
        gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        avg_gap = round(sum(gaps) / len(gaps)) if gaps else None
        lifetime = sum(float(b.get("amount") or 0) for b in bl)
        last90 = [b for b in bl if (last - timedelta(days=90)).isoformat() <= (b.get("date") or "") <= last.isoformat()]
        run_rate = round(sum(float(b.get("amount") or 0) for b in last90) / 3)
        led = compute(bl, pays_by.get(did, []))
        rows.append({"id": did, "name": d.get("name"), "area": d.get("area"), "phone": d.get("phone"),
                     "last_bill": last.isoformat(), "days_since": since, "bills": len(bl), "avg_gap": avg_gap,
                     "lifetime": round(lifetime), "monthly_run_rate": run_rate, "outstanding": led["outstanding"],
                     "regular": bool(avg_gap and avg_gap <= 45 and len(bl) >= 3),
                     "lost_revenue": round(run_rate * since / 30) if run_rate else 0})
    rows.sort(key=lambda r: (not r["regular"], -r["monthly_run_rate"]))
    return {"days": days, "rows": rows, "count": len(rows), "regular_lost": sum(1 for r in rows if r["regular"]),
            "lost_revenue": sum(r["lost_revenue"] for r in rows)}


# 7. Month-on-month --------------------------------------------------------------------------
def _month_range(mo):
    y, m = int(mo[:4]), int(mo[5:7])
    nm = date(y + (m // 12), (m % 12) + 1, 1)
    return f"{mo}-01", (nm - timedelta(days=1)).isoformat()


def _shift(mo, k):
    y, m = int(mo[:4]), int(mo[5:7])
    t = y * 12 + (m - 1) + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


@router.get("/mom")
async def month_on_month(month: str = "", by: str = "brand", company=Depends(current_company), _=Depends(admin)):
    month = month or date.today().strftime("%Y-%m")
    cost_of = await _cost_ctx(company)
    key = {"brand": "brand", "group": "group", "dealer": "dealer_name", "model": "model"}.get(by, "brand")
    periods = {"cur": month, "prev": _shift(month, -1), "ly": _shift(month, -12)}
    data = {}
    for k, mo in periods.items():
        a, b = _month_range(mo)
        tot = {"sale": 0.0, "cost": 0.0, "units": 0, "by": {}}
        for s in await _sales(company, a, b):
            amt = s.get("amount", 0) or 0; c = cost_of(s); q = 1 if s.get("imei") else (s.get("qty") or 0)
            tot["sale"] += amt; tot["cost"] += c; tot["units"] += q
            g = tot["by"].setdefault(s.get(key) or "—", {"sale": 0.0, "cost": 0.0, "units": 0})
            g["sale"] += amt; g["cost"] += c; g["units"] += q
        data[k] = tot
    names = set()
    for k in data:
        names |= set(data[k]["by"])

    def pct(a, b):
        return round((a - b) / b * 100) if b else None
    rows = []
    for n in names:
        c = data["cur"]["by"].get(n, {"sale": 0, "cost": 0, "units": 0})
        p = data["prev"]["by"].get(n, {"sale": 0, "cost": 0, "units": 0})
        l = data["ly"]["by"].get(n, {"sale": 0, "cost": 0, "units": 0})
        rows.append({"name": n, "cur": round(c["sale"]), "prev": round(p["sale"]), "ly": round(l["sale"]),
                     "cur_units": c["units"], "prev_units": p["units"], "ly_units": l["units"],
                     "cur_margin": round(c["sale"] - c["cost"]), "prev_margin": round(p["sale"] - p["cost"]),
                     "mom_pct": pct(c["sale"], p["sale"]), "yoy_pct": pct(c["sale"], l["sale"])})
    rows.sort(key=lambda r: -(r["cur"] or r["prev"]))
    tot = {k: {"sale": round(v["sale"]), "margin": round(v["sale"] - v["cost"]), "units": v["units"]} for k, v in data.items()}
    tot["mom_pct"] = pct(data["cur"]["sale"], data["prev"]["sale"]); tot["yoy_pct"] = pct(data["cur"]["sale"], data["ly"]["sale"])
    return {"month": month, "periods": periods, "by": by, "rows": rows, "totals": tot}


# 8. Category mix ----------------------------------------------------------------------------
@router.get("/category-mix")
async def category_mix(frm: str = Query(alias="from"), to: str = Query(...), brand: str = "",
                       company=Depends(current_company), _=Depends(admin)):
    cost_of = await _cost_ctx(company)
    cats, tot_sale, tot_cost, brands = {}, 0.0, 0.0, set()
    for s in await _sales(company, frm, to, brand):
        brands.add(s.get("brand") or "—")
        amt = s.get("amount", 0) or 0; c = cost_of(s); q = 1 if s.get("imei") else (s.get("qty") or 0)
        g = cats.setdefault(s.get("group") or s.get("sub_group") or "Other", {"sale": 0.0, "cost": 0.0, "units": 0, "models": set()})
        g["sale"] += amt; g["cost"] += c; g["units"] += q; g["models"].add(s.get("model"))
        tot_sale += amt; tot_cost += c
    rows = []
    for k, v in cats.items():
        gm = v["sale"] - v["cost"]
        rows.append({"group": k, "sale": round(v["sale"]), "cost": round(v["cost"]), "margin": round(gm), "units": v["units"],
                     "models": len(v["models"]), "rev_share": round(v["sale"] / tot_sale * 100, 1) if tot_sale else 0,
                     "margin_share": round(gm / (tot_sale - tot_cost) * 100, 1) if (tot_sale - tot_cost) else 0,
                     "margin_pct": round(gm / v["sale"] * 100, 1) if v["sale"] else 0,
                     "per_unit_margin": round(gm / v["units"]) if v["units"] else 0})
    rows.sort(key=lambda r: -r["sale"])
    all_brands = sorted(b for b in await db.sales.distinct("brand", {"company_id": company}) if b)
    return {"from": frm, "to": to, "brand": brand.upper(), "brands": all_brands, "rows": rows,
            "total_sale": round(tot_sale), "total_margin": round(tot_sale - tot_cost),
            "margin_pct": round((tot_sale - tot_cost) / tot_sale * 100, 1) if tot_sale else 0}


# 9. Price realisation -----------------------------------------------------------------------
@router.get("/price-realisation")
async def price_realisation(frm: str = Query(alias="from"), to: str = Query(...), brand: str = "",
                            company=Depends(current_company), _=Depends(admin)):
    cost_of = await _cost_ctx(company)
    models = {}
    for s in await _sales(company, frm, to, brand):
        q = 1 if s.get("imei") else (s.get("qty") or 0)
        if q <= 0:
            continue
        rate = (s.get("amount", 0) or 0) / q
        m = models.setdefault(s.get("model") or "—", {"brand": s.get("brand"), "group": s.get("group"), "qty": 0, "sale": 0.0, "cost": 0.0,
                                                        "min": rate, "max": rate, "dealers": {}})
        m["qty"] += q; m["sale"] += rate * q; m["cost"] += cost_of(s)
        m["min"] = min(m["min"], rate); m["max"] = max(m["max"], rate)
        dd = m["dealers"].setdefault(s.get("dealer_name") or "—", {"qty": 0, "amt": 0.0})
        dd["qty"] += q; dd["amt"] += rate * q
    rows = []
    for k, m in models.items():
        avg = m["sale"] / m["qty"]; pcost = m["cost"] / m["qty"] if m["qty"] else 0
        cheapest = min(m["dealers"].items(), key=lambda x: x[1]["amt"] / x[1]["qty"]) if m["dealers"] else None
        rows.append({"model": k, "brand": m["brand"], "group": m["group"], "qty": m["qty"],
                     "avg_sale": round(avg), "avg_cost": round(pcost), "min_sale": round(m["min"]), "max_sale": round(m["max"]),
                     "spread_pct": round((m["max"] - m["min"]) / avg * 100, 1) if avg else 0,
                     "margin_pct": round((avg - pcost) / avg * 100, 1) if avg else 0,
                     "margin": round(m["sale"] - m["cost"]),
                     "leak": round((avg - m["min"]) * m["qty"]) if m["qty"] else 0,
                     "cheapest_dealer": cheapest[0] if cheapest else None,
                     "cheapest_rate": round(cheapest[1]["amt"] / cheapest[1]["qty"]) if cheapest else None})
    rows.sort(key=lambda r: r["margin_pct"])
    all_brands = sorted(b for b in await db.sales.distinct("brand", {"company_id": company}) if b)
    return {"from": frm, "to": to, "brand": brand.upper(), "brands": all_brands, "rows": rows,
            "below_cost": sum(1 for r in rows if r["margin_pct"] < 0),
            "total_leak": sum(r["leak"] for r in rows)}


# 10. Cash-flow forecast ---------------------------------------------------------------------
@router.get("/cashflow")
async def cashflow_forecast(company=Depends(current_company), _=Depends(admin)):
    dealers, bills_by, pays_by = await _ledger_ctx(company)
    today = date.today()
    weeks = [{"label": "This week", "from": 0, "to": 7, "amount": 0.0},
             {"label": "Week 2", "from": 8, "to": 14, "amount": 0.0},
             {"label": "Week 3", "from": 15, "to": 21, "amount": 0.0},
             {"label": "Week 4", "from": 22, "to": 30, "amount": 0.0}]
    beyond = at_risk = 0.0
    rows = []
    all_delays = []
    per_dealer = {}
    for did, d in dealers.items():
        bl, pz = bills_by.get(did, []), pays_by.get(did, [])
        f = _fifo(bl, pz, today)
        paid = [x for x in f if x["paid"]]
        w = sum(float(x["bill"].get("amount") or 0) for x in paid)
        if w:
            per_dealer[did] = sum(x["delay"] * float(x["bill"].get("amount") or 0) for x in paid) / w
            all_delays.append(per_dealer[did])
    global_delay = sum(all_delays) / len(all_delays) if all_delays else 30
    for did, d in dealers.items():
        bl, pz = bills_by.get(did, []), pays_by.get(did, [])
        unpaid = bill_breakdown(bl, pz, today)
        if not unpaid:
            continue
        delay = per_dealer.get(did, global_delay)
        exp = {"w1": 0.0, "w2": 0.0, "w3": 0.0, "w4": 0.0, "beyond": 0.0, "risk": 0.0}
        for b in unpaid:
            eta = round(delay) - b["days"]        # days from today until expected payment
            amt = b["unpaid"]
            if b["days"] > delay + 45:
                exp["risk"] += amt; at_risk += amt; continue
            eta = max(eta, 0) if eta < 0 else eta
            if eta < 0:
                eta = 0
            if eta <= 7:
                exp["w1"] += amt; weeks[0]["amount"] += amt
            elif eta <= 14:
                exp["w2"] += amt; weeks[1]["amount"] += amt
            elif eta <= 21:
                exp["w3"] += amt; weeks[2]["amount"] += amt
            elif eta <= 30:
                exp["w4"] += amt; weeks[3]["amount"] += amt
            else:
                exp["beyond"] += amt; beyond += amt
        rows.append({"id": did, "name": d.get("name"), "phone": d.get("phone"), "avg_delay": round(delay),
                     "has_history": did in per_dealer, "outstanding": round(sum(b["unpaid"] for b in unpaid)),
                     **{k: round(v) for k, v in exp.items()}})
    rows.sort(key=lambda r: -(r["w1"] + r["w2"]))
    for w in weeks:
        w["amount"] = round(w["amount"])
    return {"as_of": today.isoformat(), "weeks": weeks, "next30": sum(w["amount"] for w in weeks),
            "beyond": round(beyond), "at_risk": round(at_risk), "global_delay": round(global_delay), "rows": rows}


# 11. Collector efficiency ------------------------------------------------------------------
@router.get("/collector-efficiency")
async def collector_efficiency(frm: str = Query(alias="from"), to: str = Query(...), commission_pct: float = 0,
                               company=Depends(current_company), _=Depends(admin)):
    dealers, bills_by, pays_by = await _ledger_ctx(company)
    who = {}
    async for u in db.users.find({"role": "collector"}):
        if company in (u.get("company_ids") or []) or not u.get("company_ids"):
            who[u["_id"]] = u.get("name")
    stats = {}

    def st(uid, name=None):
        s = stats.setdefault(uid, {"name": who.get(uid) or name or "—", "assigned": 0, "outstanding": 0.0, "opening": 0.0,
                                   "collected": 0.0, "receipts": 0, "visits": 0, "visited": set(), "cheques": 0, "bounced": 0})
        if name and s["name"] == "—":
            s["name"] = name
        return s
    for did, d in dealers.items():
        cid = d.get("collector_id")
        if not cid:
            continue
        s = st(cid)
        s["assigned"] += 1
        s["outstanding"] += compute(bills_by.get(did, []), pays_by.get(did, []))["outstanding"]
        before = [p for p in pays_by.get(did, []) if (p.get("date") or "") < frm]
        s["opening"] += compute([b for b in bills_by.get(did, []) if (b.get("date") or "") < frm], before)["outstanding"]
    async for p in db.payments.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
        cid = p.get("collector_id")
        if not cid or cid == "seed":
            continue
        s = st(cid, p.get("collector_name"))
        if p.get("status") == "bounced":
            s["bounced"] += 1; continue
        if p.get("approved", True) is False:
            continue
        s["collected"] += p.get("amount", 0) or 0; s["receipts"] += 1
        if p.get("mode") == "Cheque":
            s["cheques"] += 1
    async for v in db.visits.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
        s = st(v.get("user_id"), v.get("user_name"))
        s["visits"] += 1; s["visited"].add(v.get("dealer_id"))
    rows = []
    for uid, s in stats.items():
        base = s["opening"] + s["collected"]
        rows.append({"id": uid, "collector": s["name"], "assigned": s["assigned"], "outstanding": round(s["outstanding"]),
                     "collected": round(s["collected"]), "receipts": s["receipts"], "visits": s["visits"],
                     "visited": len(s["visited"]), "cheques": s["cheques"], "bounced": s["bounced"],
                     "per_visit": round(s["collected"] / s["visits"]) if s["visits"] else 0,
                     "recovery_pct": round(s["collected"] / base * 100, 1) if base else 0,
                     "coverage_pct": round(len(s["visited"]) / s["assigned"] * 100) if s["assigned"] else 0,
                     "commission": round(s["collected"] * commission_pct / 100)})
    rows.sort(key=lambda r: -r["collected"])
    return {"from": frm, "to": to, "commission_pct": commission_pct, "rows": rows,
            "totals": {"collected": sum(r["collected"] for r in rows), "outstanding": sum(r["outstanding"] for r in rows),
                       "visits": sum(r["visits"] for r in rows), "commission": sum(r["commission"] for r in rows)}}


# 12. Scheme tracker ------------------------------------------------------------------------
class SchemeIn(BaseModel):
    month: str
    basis: str = "purchase"     # purchase (default: Haier pays on what you buy) | sale
    brand: str = ""
    scope: str = "all"          # all | group | model
    scope_value: str = ""
    target_qty: int = 0
    target_amount: float = 0
    payout_pct: float = 0       # % of achieved sale value, paid when target met
    payout_amount: float = 0    # flat amount, paid when target met
    prorata: bool = False       # pay proportionally below target
    note: str = ""


class SchemeStatus(BaseModel):
    status: str                 # open | claimed | received
    received_amount: float = 0
    received_on: str = ""
    note: str = ""


def _scheme_pub(s):
    s = dict(s); s["id"] = s.pop("_id"); s.pop("company_id", None); return s


async def _purchases(company, frm, to):
    return [p async for p in db.purchases.find({"company_id": company, "date": {"$gte": frm, "$lte": to}})]


async def scheme_achievement(company, month):
    a, b = _month_range(month)
    sales = await _sales(company, a, b)
    purchases = await _purchases(company, a, b)
    d1 = _parse(a); d2 = min(_parse(b), date.today())
    progress = max(1, (d2 - d1).days + 1) / ((_parse(b) - d1).days + 1) * 100
    out = []
    async for sc in db.schemes.find({"company_id": company, "month": month}):
        qty = amt = 0.0
        pool = purchases if (sc.get("basis") or "purchase") == "purchase" else sales
        for s in pool:
            if sc.get("brand") and not brand_match(s, sc["brand"]):
                continue
            if sc.get("scope") == "group" and (s.get("group") or "") != sc.get("scope_value"):
                continue
            if sc.get("scope") == "model" and (s.get("model") or "") != sc.get("scope_value"):
                continue
            qty += 1 if s.get("imei") else (s.get("qty") or 0); amt += s.get("amount", 0) or 0
        tq, ta = sc.get("target_qty") or 0, sc.get("target_amount") or 0
        ach = []
        if tq:
            ach.append(qty / tq)
        if ta:
            ach.append(amt / ta)
        no_target = not ach
        pct = min(ach) if ach else 1.0
        pct_income = amt * (sc.get("payout_pct", 0) / 100)          # % on purchase/sale value (always accrues)
        full = sc.get("payout_amount", 0) + pct_income
        if no_target:
            earned = pct_income
        else:
            earned = full if pct >= 1 else (full * pct if sc.get("prorata") else pct_income * 0)
        gap_qty = max(0, tq - qty) if tq else 0
        gap_amt = max(0, ta - amt) if ta else 0
        behind = (not no_target) and pct < 1 and (pct * 100) < progress - 5
        out.append({**_scheme_pub(sc), "basis": sc.get("basis") or "purchase", "status": sc.get("status") or "open",
                    "received_amount": sc.get("received_amount") or 0, "received_on": sc.get("received_on"),
                    "actual_qty": int(qty), "actual_amount": round(amt), "achieved_pct": round(pct * 100, 1),
                    "pct_income": round(pct_income), "earned": round(earned),
                    "potential": round(full if pct >= 1 else (sc.get("payout_amount", 0) + max(amt, ta) * sc.get("payout_pct", 0) / 100)),
                    "gap_qty": gap_qty, "gap_amount": round(gap_amt), "met": no_target or pct >= 1, "no_target": no_target,
                    "behind": behind, "needed_pace": round((pct * 100) - progress, 1)})
    return out


@router.get("/schemes")
async def schemes_list(month: str = "", company=Depends(current_company), _=Depends(admin)):
    month = month or date.today().strftime("%Y-%m")
    rows = await scheme_achievement(company, month)
    a, b = _month_range(month)
    d1 = _parse(a); d2 = min(_parse(b), date.today())
    elapsed = max(1, (d2 - d1).days + 1); total_days = (_parse(b) - d1).days + 1
    groups = sorted(g for g in await db.sales.distinct("group", {"company_id": company}) if g)
    models = sorted(m for m in await db.sales.distinct("model", {"company_id": company}) if m)
    brands = sorted(x for x in await db.sales.distinct("brand", {"company_id": company}) if x)
    return {"month": month, "rows": rows, "earned": sum(r["earned"] for r in rows), "potential": sum(r["potential"] for r in rows),
            "received": sum(r["received_amount"] for r in rows), "behind": sum(1 for r in rows if r["behind"]),
            "pending_claim": sum(r["earned"] for r in rows if r["status"] == "open" and r["earned"] > 0),
            "month_progress_pct": round(elapsed / total_days * 100), "groups": groups, "models": models, "brands": brands}


@router.post("/schemes")
async def scheme_create(body: SchemeIn, company=Depends(current_company), _=Depends(admin)):
    if len(body.month) != 7:
        raise HTTPException(400, "month must be YYYY-MM")
    if not (body.target_qty or body.target_amount or body.payout_pct or body.payout_amount):
        raise HTTPException(400, "Set a target, or a % / flat payout")
    if body.basis not in ("purchase", "sale"):
        raise HTTPException(400, "basis must be purchase or sale")
    doc = {"_id": uuid.uuid4().hex, "company_id": company, **body.model_dump(), "brand": body.brand.upper(),
           "status": "open", "received_amount": 0, "created_at": datetime.now().isoformat()}
    await db.schemes.insert_one(doc)
    return _scheme_pub(doc)


@router.patch("/schemes/{sid}/status")
async def scheme_status(sid: str, body: SchemeStatus, company=Depends(current_company), _=Depends(admin)):
    if body.status not in ("open", "claimed", "received"):
        raise HTTPException(400, "status must be open, claimed or received")
    upd = {"status": body.status, "received_amount": body.received_amount if body.status == "received" else 0,
           "received_on": (body.received_on or date.today().isoformat()) if body.status == "received" else None}
    if body.note:
        upd["status_note"] = body.note
    r = await db.schemes.update_one({"_id": sid, "company_id": company}, {"$set": upd})
    if not r.matched_count:
        raise HTTPException(404, "Scheme not found")
    return {"ok": True}


@router.delete("/schemes/{sid}")
async def scheme_delete(sid: str, company=Depends(current_company), _=Depends(admin)):
    r = await db.schemes.delete_one({"_id": sid, "company_id": company})
    if not r.deleted_count:
        raise HTTPException(404, "Scheme not found")
    return {"ok": True}


@router.get("/schemes/earned")
async def schemes_earned(frm: str = Query(alias="from"), to: str = Query(...), brand: str = "",
                         company=Depends(current_company), _=Depends(admin)):
    """Scheme payout earned across all months touching the range (used by Profit 2)."""
    mo, end = frm[:7], to[:7]
    total, n = 0.0, 0
    while mo <= end:
        for r in await scheme_achievement(company, mo):
            if brand and (r.get("brand") or "").upper() not in ("", brand.upper()):
                continue
            total += r["earned"]; n += 1
        mo = _shift(mo, 1)
    return {"earned": round(total), "schemes": n}


# 13. Daily digest ---------------------------------------------------------------------------
@router.get("/digest")
async def daily_digest(day: str = "", company=Depends(current_company), _=Depends(admin)):
    day = day or (date.today() - timedelta(days=1)).isoformat()
    dealers, bills_by, pays_by = await _ledger_ctx(company)
    sales = await _sales(company, day, day)
    sale_total = sum(s.get("amount", 0) or 0 for s in sales)
    sale_units = sum(1 if s.get("imei") else (s.get("qty") or 0) for s in sales)
    bill_total = sum(float(b.get("amount") or 0) for bl in bills_by.values() for b in bl if b.get("date") == day)
    bill_count = sum(1 for bl in bills_by.values() for b in bl if b.get("date") == day)
    by_dealer = {}
    for s in sales:
        by_dealer[s.get("dealer_name") or "—"] = by_dealer.get(s.get("dealer_name") or "—", 0) + (s.get("amount", 0) or 0)
    coll, coll_rows = 0.0, []
    async for p in db.payments.find({"company_id": company, "date": day}):
        if not _live(p) or p.get("collector_id") in (None, "seed"):
            continue
        coll += p.get("amount", 0) or 0
        coll_rows.append({"dealer": p.get("dealer_name"), "amount": round(p.get("amount", 0) or 0), "mode": p.get("mode"), "collector": p.get("collector_name")})
    outstanding, over90, top = 0.0, 0.0, []
    for did, d in dealers.items():
        led = compute(bills_by.get(did, []), pays_by.get(did, []))
        outstanding += led["outstanding"]; over90 += led["ageing"]["age_90p"]
        if led["outstanding"] > 0:
            top.append({"dealer": d.get("name"), "outstanding": led["outstanding"], "age_90p": led["ageing"]["age_90p"]})
    top.sort(key=lambda x: -x["age_90p"] or -x["outstanding"])
    # low stock: models sold in last 30 days with <=2 units on hand
    since = (date.today() - timedelta(days=30)).isoformat()
    sold_recent = {}
    for s in await _sales(company, since, date.today().isoformat()):
        sold_recent[s.get("model")] = sold_recent.get(s.get("model"), 0) + (1 if s.get("imei") else (s.get("qty") or 0))
    on_hand = {}
    async for u in db.stock_units.find({"company_id": company, "status": "in_stock"}, {"model": 1}):
        on_hand[u.get("model")] = on_hand.get(u.get("model"), 0) + 1
    async for lot in db.stock_lots.find({"company_id": company}):
        on_hand[lot.get("model")] = on_hand.get(lot.get("model"), 0) + max(0, (lot.get("in_qty", 0) or 0) - (lot.get("sold_qty", 0) or 0))
    low = [{"model": m, "on_hand": on_hand.get(m, 0), "sold_30d": q} for m, q in sold_recent.items() if on_hand.get(m, 0) <= 2]
    low.sort(key=lambda x: -x["sold_30d"])
    visits = await db.visits.count_documents({"company_id": company, "date": day})
    new_dealers = await db.dealers.count_documents({"company_id": company, "created_at": {"$regex": f"^{day}"}})
    return {"day": day, "sales": {"amount": round(sale_total), "units": sale_units, "lines": len(sales), "bills": bill_count, "bill_amount": round(bill_total),
                                  "by_dealer": sorted([{"dealer": k, "amount": round(v)} for k, v in by_dealer.items()], key=lambda x: -x["amount"])[:8]},
            "collections": {"amount": round(coll), "receipts": len(coll_rows), "rows": coll_rows[:10]},
            "outstanding": round(outstanding), "over90": round(over90), "top_overdue": top[:5],
            "low_stock": low[:8], "visits": visits, "new_dealers": new_dealers}


# Admin dashboard ---------------------------------------------------------------------------
async def dashboard_access(user=Depends(get_current_user)):
    if user["role"] == "admin" or user.get("can_view_dashboard"):
        return user
    raise HTTPException(403, "You do not have dashboard access")


@router.get("/dashboard")
async def admin_dashboard(company=Depends(current_company), _=Depends(dashboard_access)):
    today = date.today(); tiso = today.isoformat()
    m_start = tiso[:7] + "-01"
    dealers, bills_by, pays_by = await _ledger_ctx(company)
    cost_of = await _cost_ctx(company)
    # today & MTD sales
    def agg(sales):
        amt = sum(s.get("amount", 0) or 0 for s in sales); cost = sum(cost_of(s) for s in sales)
        return {"amount": round(amt), "units": sum(1 if s.get("imei") else (s.get("qty") or 0) for s in sales), "margin": round(amt - cost),
                "margin_pct": round((amt - cost) / amt * 100, 1) if amt else 0}
    mtd_sales = await _sales(company, m_start, tiso)
    today_sales = [s for s in mtd_sales if s.get("date") == tiso]
    # collections today / MTD
    coll_today = coll_mtd = 0.0
    async for p in db.payments.find({"company_id": company, "date": {"$gte": m_start, "$lte": tiso}}):
        if not _live(p) or p.get("collector_id") in (None, "seed"):
            continue
        coll_mtd += p.get("amount", 0) or 0
        if p.get("date") == tiso:
            coll_today += p.get("amount", 0) or 0
    # outstanding + cash forecast (reuse cashflow)
    cf = await cashflow_forecast(company=company, _=None)
    outstanding = sum(compute(bills_by.get(d, []), pays_by.get(d, []))["outstanding"] for d in dealers)
    over90 = sum(compute(bills_by.get(d, []), pays_by.get(d, []))["ageing"]["age_90p"] for d in dealers)
    # slowing dealers
    tr = await credit_trend(months=6, company=company, _=None)
    slowing = [{"name": r["name"], "latest": r["latest"], "change": r["change"], "outstanding": r["outstanding"]} for r in tr["rows"] if r["flag"] == "warning"][:5]
    # inactive
    ina = await inactive_dealers(days=30, company=company, _=None)
    # schemes
    sch = await schemes_list(month=tiso[:7], company=company, _=None)
    # low stock (from digest logic)
    dg = await daily_digest(day=tiso, company=company, _=None)
    # pending approvals & cheques
    pending_appr = await db.payments.count_documents({"company_id": company, "approved": False})
    cheques = []
    async for p in db.payments.find({"company_id": company, "mode": "Cheque", "status": "pending"}):
        cheques.append({"dealer": p.get("dealer_name"), "amount": round(p.get("amount", 0) or 0), "cheque": p.get("cheque"),
                        "cheque_date": p.get("cheque_date"), "due": (p.get("cheque_date") or p.get("date") or "") <= tiso})
    cheques.sort(key=lambda c: c["cheque_date"] or c.get("date") or "")
    due_today = [c for c in cheques if c["cheque_date"] == tiso]
    overdue = [c for c in cheques if c["cheque_date"] and c["cheque_date"] < tiso]
    scheme_alerts = [{"label": (r.get("brand") or "All") + " · " + (r["scope_value"] if r.get("scope") != "all" else "All models"),
                      "achieved_pct": r["achieved_pct"], "gap_qty": r["gap_qty"], "gap_amount": r["gap_amount"],
                      "potential": r["potential"], "basis": r["basis"]} for r in sch["rows"] if r["behind"]]
    # stock value
    stock_value = 0.0; stock_units = 0
    async for u in db.stock_units.find({"company_id": company, "status": "in_stock"}, {"purchase_rate": 1}):
        stock_value += u.get("purchase_rate") or 0; stock_units += 1
    top_dealers_mtd = {}
    for s in mtd_sales:
        top_dealers_mtd[s.get("dealer_name") or "—"] = top_dealers_mtd.get(s.get("dealer_name") or "—", 0) + (s.get("amount", 0) or 0)
    return {"date": tiso,
            "today": {"sales": agg(today_sales), "collections": round(coll_today)},
            "mtd": {"sales": agg(mtd_sales), "collections": round(coll_mtd),
                    "top_dealers": sorted([{"dealer": k, "amount": round(v)} for k, v in top_dealers_mtd.items()], key=lambda x: -x["amount"])[:5]},
            "outstanding": round(outstanding), "over90": round(over90),
            "forecast": {"week1": cf["weeks"][0]["amount"], "next30": cf["next30"], "at_risk": cf["at_risk"]},
            "slowing": slowing, "slowing_count": tr["warnings"],
            "inactive": {"count": ina["count"], "regular_lost": ina["regular_lost"], "lost_revenue": ina["lost_revenue"],
                         "rows": [{"name": r["name"], "days_since": r["days_since"], "run_rate": r["monthly_run_rate"]} for r in ina["rows"][:5]]},
            "schemes": {"earned": sch["earned"], "potential": sch["potential"], "progress": sch["month_progress_pct"],
                        "rows": [{"label": (r.get("brand") or "All") + " · " + (r["scope_value"] if r.get("scope") != "all" else "All models"),
                                  "achieved_pct": r["achieved_pct"], "met": r["met"], "behind": r["behind"], "basis": r["basis"], "gap_qty": r["gap_qty"], "gap_amount": r["gap_amount"]} for r in sch["rows"]],
                        "alerts": scheme_alerts},
            "cheque_alerts": {"due_today": due_today, "overdue": overdue, "due_today_total": sum(c["amount"] for c in due_today), "overdue_total": sum(c["amount"] for c in overdue)},
            "low_stock": dg["low_stock"][:6],
            "stock": {"value": round(stock_value), "units": stock_units},
            "pending_approvals": pending_appr,
            "cheques": {"due": sum(c["amount"] for c in cheques if c["due"]), "future": sum(c["amount"] for c in cheques if not c["due"]), "rows": cheques[:6]},
            "top_overdue": dg["top_overdue"][:5]}
