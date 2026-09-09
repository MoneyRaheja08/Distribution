import csv
import io
import re
import uuid
from datetime import datetime, timezone, date, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from ..auth import get_current_user, require_roles
from ..db import db
from ..deps import current_company

router = APIRouter(tags=["catalog"])
staff_only = require_roles("admin", "manager")


def _date_iso(s: str):
    s = (s or "").strip()
    if not s:
        return None
    sep = "/" if "/" in s else ("-" if "-" in s else None)
    if not sep:
        return s
    parts = s.split(sep)
    if len(parts) != 3:
        return s
    a, b, c = parts
    if len(a) == 4:                       # YYYY-MM-DD
        return f"{a}-{b.zfill(2)}-{c.zfill(2)}"
    if len(c) == 2:
        c = "20" + c
    return f"{c}-{b.zfill(2)}-{a.zfill(2)}"  # DD?MM?YYYY -> ISO


def _num(v, default=0.0):
    try:
        return float(str(v or "").replace(",", "").strip() or 0)
    except ValueError:
        return default


def _parse_csv(data: bytes):
    """Parse a MARG-style sale/purchase CSV (skips any title preamble)."""
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    hidx = None
    for i, r in enumerate(rows):
        if r and r[0].strip().upper().rstrip(".") == "BILL NO":
            hidx = i
            break
    if hidx is None:
        raise HTTPException(400, "Could not find a header row (BILL NO.) in this CSV")
    header = [c.strip().upper().rstrip(".") for c in rows[hidx]]
    idx = {h: i for i, h in enumerate(header)}

    def col(r, *names):
        for n in names:
            j = idx.get(n.upper())
            if j is not None and j < len(r):
                return (r[j] or "").strip()
        return ""

    lines = []
    for r in rows[hidx + 1:]:
        if not any((c or "").strip() for c in r):
            continue
        bill = col(r, "BILL NO")
        if not bill:
            continue
        qty = int(_num(col(r, "QTY"), 0)) or 0
        rate = _num(col(r, "RATE"))
        amt_raw = col(r, "AMOUNT")
        amount = _num(amt_raw) if amt_raw else round(qty * rate, 2)
        lines.append({
            "bill_no": bill,
            "date": _date_iso(col(r, "BILL DATE")),
            "party": col(r, "PARTY NAME"),
            "brand": col(r, "COMPANY").upper(),
            "group": col(r, "GROUP"),
            "sub_group": col(r, "SUB GROUP"),
            "model": col(r, "ITEM DETAILS"),
            "godown": col(r, "GODOWN"),
            "qty": qty,
            "rate": rate,
            "amount": amount,
            "imei": col(r, "IMEI").strip(),
            "supplier": col(r, "PARTY NAME"),
        })
    if not lines:
        raise HTTPException(422, "No data rows found in this CSV")
    return lines


# ---------------- Sale import ----------------
def _ph(s):
    d = "".join(c for c in (s or "") if c.isdigit())
    return d[-10:] if len(d) >= 10 else d


def _match_dealer(by_name, by_phone, party, mobile):
    return by_name.get((party or "").strip().lower()) or (by_phone.get(_ph(mobile)) if _ph(mobile) else None)


async def _sale_context(company):
    dealers = [d async for d in db.dealers.find({"company_id": company})]
    by_name = {d["name"].strip().lower(): d for d in dealers}
    by_phone = {_ph(d.get("phone")): d for d in dealers if _ph(d.get("phone"))}
    existing = set()
    async for b in db.bills.find({"company_id": company}, {"dealer_id": 1, "bill_no": 1}):
        existing.add((b["dealer_id"], (b.get("bill_no") or "").strip().lower()))
    return by_name, by_phone, existing


@router.post("/import/sale/preview")
async def sale_preview(file: UploadFile = File(...), company=Depends(current_company), _=Depends(staff_only)):
    lines = _parse_csv(await file.read())
    by_name, by_phone, existing = await _sale_context(company)
    groups = {}
    for ln in lines:
        g = groups.setdefault(ln["bill_no"], {"bill_no": ln["bill_no"], "date": ln["date"],
                                              "party": ln["party"], "mobile": ln["mobile"], "total": 0.0, "lines": 0, "units": 0})
        g["total"] += ln["amount"]
        g["lines"] += 1
        if ln["imei"]:
            g["units"] += 1
    out, matched, unmatched, dup, matched_total = [], 0, 0, 0, 0.0
    for g in groups.values():
        d = _match_dealer(by_name, by_phone, g["party"], g.get("mobile"))
        is_dup = d is not None and (d["_id"], g["bill_no"].strip().lower()) in existing
        out.append({**g, "total": round(g["total"], 2), "matched": d is not None,
                    "dealer": d["name"] if d else None, "duplicate": is_dup})
        if d is None:
            unmatched += 1
        elif is_dup:
            dup += 1
        else:
            matched += 1
            matched_total += g["total"]
    out.sort(key=lambda x: x["bill_no"])
    sale_imeis = [l["imei"] for l in lines if l["imei"]]
    known = set()
    if sale_imeis:
        async for u in db.stock_units.find({"company_id": company, "imei": {"$in": sale_imeis}}, {"imei": 1}):
            known.add(u["imei"])
    unknown = [i for i in sale_imeis if i not in known]
    return {"brand": lines[0]["brand"] if lines else "", "bills": out,
            "summary": {"total_bills": len(out), "matched": matched, "unmatched": unmatched,
                        "duplicates": dup, "matched_total": round(matched_total, 2),
                        "total_units": sum(1 for l in lines if l["imei"]), "total_lines": len(lines),
                        "unknown_serials": len(unknown), "unknown_sample": unknown[:8]}}


@router.post("/import/sale/commit")
async def sale_commit(file: UploadFile = File(...), company=Depends(current_company), _=Depends(staff_only)):
    lines = _parse_csv(await file.read())
    by_name, by_phone, existing = await _sale_context(company)
    now = datetime.now(timezone.utc).isoformat()
    groups = {}
    for ln in lines:
        groups.setdefault(ln["bill_no"], []).append(ln)
    bills_added = skipped_unmatched = skipped_dup = 0
    bill_docs = []
    for bill_no, glines in groups.items():
        d = _match_dealer(by_name, by_phone, glines[0]["party"], glines[0].get("mobile"))
        if not d:
            skipped_unmatched += 1
            continue
        key = (d["_id"], bill_no.strip().lower())
        if key in existing:
            skipped_dup += 1
            continue
        existing.add(key)
        bill_docs.append({"_id": uuid.uuid4().hex, "dealer_id": d["_id"], "bill_no": bill_no,
                          "date": glines[0]["date"], "amount": round(sum(l["amount"] for l in glines), 2),
                          "source": "sale_csv", "company_id": company})
        bills_added += 1
    if bill_docs:
        await db.bills.insert_many(bill_docs)

    units_sold = qty_sold = 0
    sale_docs = []
    for ln in lines:
        d = _match_dealer(by_name, by_phone, ln["party"], ln.get("mobile"))
        sale_docs.append({"_id": uuid.uuid4().hex, "company_id": company, "brand": ln["brand"], "group": ln["group"],
                          "sub_group": ln["sub_group"], "model": ln["model"], "godown": ln["godown"], "qty": ln["qty"],
                          "rate": ln["rate"], "amount": ln["amount"], "imei": ln["imei"] or None,
                          "dealer_id": d["_id"] if d else None, "dealer_name": ln["party"],
                          "bill_no": ln["bill_no"], "date": ln["date"], "created_at": now})
        if ln["imei"]:
            await db.stock_units.update_one(
                {"company_id": company, "imei": ln["imei"]},
                {"$set": {"status": "sold", "sale_bill": ln["bill_no"], "sale_dealer_id": d["_id"] if d else None,
                          "sale_dealer_name": ln["party"], "sale_rate": ln["rate"], "sale_date": ln["date"]},
                 "$setOnInsert": {"_id": uuid.uuid4().hex, "company_id": company, "imei": ln["imei"], "brand": ln["brand"],
                                  "group": ln["group"], "sub_group": ln["sub_group"], "model": ln["model"],
                                  "godown": ln["godown"], "created_at": now}},
                upsert=True)
            units_sold += 1
        else:
            await db.stock_lots.update_one(
                {"company_id": company, "brand": ln["brand"], "model": ln["model"]},
                {"$inc": {"sold_qty": ln["qty"]},
                 "$setOnInsert": {"_id": uuid.uuid4().hex, "company_id": company, "brand": ln["brand"],
                                  "group": ln["group"], "sub_group": ln["sub_group"], "model": ln["model"], "in_qty": 0}},
                upsert=True)
            qty_sold += ln["qty"]
    if sale_docs:
        await db.sales.insert_many(sale_docs)
    return {"ok": True, "bills_added": bills_added, "skipped_unmatched": skipped_unmatched,
            "skipped_duplicates": skipped_dup, "units_sold": units_sold, "qty_sold": qty_sold,
            "sales_lines": len(sale_docs)}


# ---------------- Purchase import ----------------
@router.post("/import/purchase/preview")
async def purchase_preview(file: UploadFile = File(...), company=Depends(current_company), _=Depends(staff_only)):
    lines = _parse_csv(await file.read())
    imeis = [l["imei"] for l in lines if l["imei"]]
    dup = set()
    if imeis:
        async for u in db.stock_units.find({"company_id": company, "imei": {"$in": imeis}}, {"imei": 1}):
            dup.add(u["imei"])
    dates = [l["date"] for l in lines if l["date"]]
    cats = {}
    for l in lines:
        name = l["group"] or l["sub_group"] or "Other"
        c = cats.setdefault(name, {"group": name, "qty": 0, "amount": 0.0})
        c["qty"] += l["qty"] or 1
        c["amount"] += l["amount"]
    return {"brand": lines[0]["brand"] if lines else "", "supplier": lines[0]["supplier"] if lines else "",
            "summary": {"lines": len(lines), "imei_units": len(imeis),
                        "qty_only": sum(l["qty"] for l in lines if not l["imei"]),
                        "total": round(sum(l["amount"] for l in lines), 2), "duplicates": len(dup),
                        "date_from": min(dates) if dates else None, "date_to": max(dates) if dates else None},
            "categories": sorted([{**c, "amount": round(c["amount"], 2)} for c in cats.values()],
                                 key=lambda x: -x["amount"])}


@router.post("/import/purchase/commit")
async def purchase_commit(file: UploadFile = File(...), company=Depends(current_company), _=Depends(staff_only)):
    lines = _parse_csv(await file.read())
    now = datetime.now(timezone.utc).isoformat()
    imeis = [l["imei"] for l in lines if l["imei"]]
    existing = set()
    if imeis:
        async for u in db.stock_units.find({"company_id": company, "imei": {"$in": imeis}}, {"imei": 1}):
            existing.add(u["imei"])
    units_added = dupes = qty_added = 0
    unit_docs, purchase_docs = [], []
    for l in lines:
        purchase_docs.append({"_id": uuid.uuid4().hex, "company_id": company, "brand": l["brand"], "group": l["group"],
                              "sub_group": l["sub_group"], "model": l["model"], "godown": l["godown"], "qty": l["qty"],
                              "rate": l["rate"], "amount": l["amount"], "imei": l["imei"] or None,
                              "supplier": l["supplier"], "bill_no": l["bill_no"], "date": l["date"], "created_at": now})
        if l["imei"]:
            if l["imei"] in existing:
                dupes += 1
                continue
            existing.add(l["imei"])
            unit_docs.append({"_id": uuid.uuid4().hex, "company_id": company, "imei": l["imei"], "brand": l["brand"],
                              "group": l["group"], "sub_group": l["sub_group"], "model": l["model"], "godown": l["godown"],
                              "status": "in_stock", "purchase_bill": l["bill_no"], "purchase_rate": l["rate"],
                              "purchase_date": l["date"], "supplier": l["supplier"], "created_at": now})
            units_added += 1
        else:
            await db.stock_lots.update_one(
                {"company_id": company, "brand": l["brand"], "model": l["model"]},
                {"$inc": {"in_qty": l["qty"]},
                 "$setOnInsert": {"_id": uuid.uuid4().hex, "company_id": company, "brand": l["brand"],
                                  "group": l["group"], "sub_group": l["sub_group"], "model": l["model"], "sold_qty": 0}},
                upsert=True)
            qty_added += l["qty"]
    if unit_docs:
        await db.stock_units.insert_many(unit_docs)
    if purchase_docs:
        await db.purchases.insert_many(purchase_docs)
    return {"ok": True, "units_added": units_added, "qty_added": qty_added, "duplicates": dupes,
            "purchase_lines": len(purchase_docs)}


# ---------------- Catalog / IMEI ----------------
@router.get("/catalog/units")
async def catalog_units(status: str = "", q: str = "", brand: str = "",
                        company=Depends(current_company), _=Depends(get_current_user)):
    query = {"company_id": company}
    if status:
        query["status"] = status
    if brand:
        query["brand"] = brand.upper()
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        query["$or"] = [{"imei": rx}, {"model": rx}]
    out = []
    async for u in db.stock_units.find(query).sort("created_at", -1).limit(500):
        u.pop("_id", None)
        out.append(u)
    return out


@router.get("/catalog/imei/{imei}")
async def imei_lookup(imei: str, company=Depends(current_company), _=Depends(get_current_user)):
    u = await db.stock_units.find_one({"company_id": company, "imei": imei.strip()})
    if not u:
        raise HTTPException(404, "IMEI/serial not found")
    u.pop("_id", None)
    return u


@router.get("/catalog/stock-summary")
async def stock_summary(company=Depends(current_company), _=Depends(get_current_user)):
    rows = []
    pipeline = [{"$match": {"company_id": company}},
                {"$group": {"_id": {"model": "$model", "brand": "$brand", "group": "$group"},
                            "total": {"$sum": 1},
                            "available": {"$sum": {"$cond": [{"$eq": ["$status", "in_stock"]}, 1, 0]}}}}]
    async for r in db.stock_units.aggregate(pipeline):
        rows.append({"model": r["_id"]["model"], "brand": r["_id"]["brand"], "group": r["_id"]["group"],
                     "total": r["total"], "available": r["available"], "tracked": "imei"})
    async for l in db.stock_lots.find({"company_id": company}):
        rows.append({"model": l["model"], "brand": l["brand"], "group": l.get("group"),
                     "total": l.get("in_qty", 0), "available": l.get("in_qty", 0) - l.get("sold_qty", 0),
                     "tracked": "qty"})
    rows.sort(key=lambda x: (x["brand"] or "", x["model"] or ""))
    val = {}
    async for u in db.stock_units.find({"company_id": company, "status": "in_stock"}):
        v = val.setdefault(u.get("brand") or "—", {"brand": u.get("brand") or "—", "units": 0, "value": 0.0})
        v["units"] += 1
        v["value"] += (u.get("purchase_rate") or 0)
    valuation = [{"brand": x["brand"], "units": x["units"], "value": round(x["value"])}
                 for x in sorted(val.values(), key=lambda x: -x["value"])]
    return {"rows": rows, "brands": sorted({r["brand"] for r in rows if r["brand"]}),
            "valuation": valuation, "total_value": round(sum(x["value"] for x in val.values()))}


@router.get("/catalog/aging-stock")
async def aging_stock(days: int = 60, company=Depends(current_company), _=Depends(get_current_user)):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    today = date.today()
    rows = []
    async for u in db.stock_units.find({"company_id": company, "status": "in_stock",
                                        "purchase_date": {"$lte": cutoff, "$ne": None}}).limit(500):
        pd = u.get("purchase_date")
        try:
            y, m, d = pd.split("-")
            age = (today - date(int(y), int(m), int(d))).days
        except Exception:
            age = None
        rows.append({"imei": u.get("imei"), "model": u.get("model"), "brand": u.get("brand"),
                     "purchase_date": pd, "days": age, "purchase_rate": round(u.get("purchase_rate") or 0)})
    rows.sort(key=lambda x: -(x["days"] or 0))
    return {"days": days, "count": len(rows), "value": round(sum(r["purchase_rate"] for r in rows)), "rows": rows}
