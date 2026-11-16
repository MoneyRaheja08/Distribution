import uuid

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_user, require_roles
from ..deps import current_company
from ..db import db
from ..models import BulkBills, BrandSplitIn, BrandImport

router = APIRouter(prefix="/bills", tags=["bills"])
staff_only = require_roles("admin", "manager")


@router.post("/bulk")
async def bulk_bills(body: BulkBills, company=Depends(current_company), _=Depends(require_roles("admin"))):
    """Add many bills at once. Rows match a dealer by id, or by name (case-insensitive)."""
    dealers = [d async for d in db.dealers.find({"company_id": company})]
    by_id = {d["_id"]: d for d in dealers}
    by_name = {d["name"].strip().lower(): d for d in dealers}
    existing = set()
    async for b in db.bills.find({}, {"dealer_id": 1, "bill_no": 1}):
        existing.add((b["dealer_id"], (b.get("bill_no") or "").strip().lower()))
    added, unmatched, duplicates = 0, [], []
    docs = []
    for r in body.bills:
        d = by_id.get(r.dealer_id) if r.dealer_id else None
        if not d and r.dealer_name:
            d = by_name.get(r.dealer_name.strip().lower())
        if not d:
            unmatched.append(r.dealer_name or r.dealer_id or "?")
            continue
        key = (d["_id"], (r.bill_no or "").strip().lower())
        if key in existing:
            duplicates.append(r.bill_no)
            continue
        existing.add(key)
        docs.append({"_id": uuid.uuid4().hex, "dealer_id": d["_id"], "bill_no": r.bill_no,
                     "date": r.date, "amount": r.amount, "source": "bulk", "company_id": company})
        added += 1
    if docs:
        await db.bills.insert_many(docs)
    return {"ok": True, "added": added, "unmatched": unmatched, "duplicates": duplicates}


@router.put("/{bid}/brands")
async def set_bill_brands(bid: str, body: BrandSplitIn, company=Depends(current_company), _=Depends(staff_only)):
    """Tag an item-less bill with the brand(s) it covers, so the Owed-by-brand report can
    attribute it. Stored on the bill only (brand_split); does not touch sales/profit reports."""
    b = await db.bills.find_one({"_id": bid, "company_id": company})
    if not b:
        raise HTTPException(404, "Bill not found")
    amt = float(b.get("amount") or 0)
    merged = {}
    for s in body.splits:
        br = (s.brand or "").strip().upper()
        if not br:
            raise HTTPException(400, "Brand name is required")
        merged[br] = merged.get(br, 0) + float(s.amount)
    total = round(sum(merged.values()))
    if total > round(amt) + 1:
        raise HTTPException(400, f"Brand amounts ({total:.0f}) exceed the bill amount ({amt:.0f})")
    split = [{"brand": k, "amount": round(v)} for k, v in merged.items()]
    if split:
        await db.bills.update_one({"_id": bid, "company_id": company}, {"$set": {"brand_split": split}})
    else:
        await db.bills.update_one({"_id": bid, "company_id": company}, {"$unset": {"brand_split": ""}})
    return {"ok": True, "bill_no": b.get("bill_no"), "brand_split": split, "untagged": round(amt) - total}


@router.post("/brand-import")
async def import_bill_brands(body: BrandImport, company=Depends(current_company), _=Depends(require_roles("admin"))):
    """Bulk-tag item-less bills with brands. Rows: Dealer, Bill No, Brand, Amount.
    Several rows for the same bill = a brand mix for that bill."""
    dealers = [d async for d in db.dealers.find({"company_id": company})]
    by_id = {d["_id"]: d for d in dealers}
    by_name = {d["name"].strip().lower(): d for d in dealers}
    bills_idx = {}
    async for b in db.bills.find({"company_id": company}, {"_id": 1, "dealer_id": 1, "bill_no": 1, "amount": 1}):
        bills_idx[(b["dealer_id"], (b.get("bill_no") or "").strip().lower())] = b
    groups, unmatched = {}, []
    for r in body.rows:
        d = by_id.get(r.dealer_id) if r.dealer_id else None
        if not d and r.dealer_name:
            d = by_name.get(r.dealer_name.strip().lower())
        key = (d["_id"], (r.bill_no or "").strip().lower()) if d else None
        bill = bills_idx.get(key) if key else None
        if not bill:
            unmatched.append((r.dealer_name or r.dealer_id or "?") + " / " + (r.bill_no or "?"))
            continue
        br = (r.brand or "").strip().upper()
        if not br:
            continue
        g = groups.setdefault(bill["_id"], {"amt": float(bill.get("amount") or 0), "split": {}, "bill_no": bill.get("bill_no")})
        g["split"][br] = g["split"].get(br, 0) + float(r.amount)
    updated, over = 0, []
    for bid2, g in groups.items():
        total = round(sum(g["split"].values()))
        if total > round(g["amt"]) + 1:
            over.append(g["bill_no"])
            continue
        split = [{"brand": k, "amount": round(v)} for k, v in g["split"].items()]
        await db.bills.update_one({"_id": bid2, "company_id": company}, {"$set": {"brand_split": split}})
        updated += 1
    return {"ok": True, "updated": updated, "unmatched": unmatched, "over_amount": over}


@router.get("/{bid}")
async def bill_detail(bid: str, company=Depends(current_company), _=Depends(get_current_user)):
    """Return a bill plus its imported line items (if it came from a Sale CSV import)."""
    b = await db.bills.find_one({"_id": bid, "company_id": company})
    if not b:
        raise HTTPException(404, "Bill not found")
    lines = []
    async for s in db.sales.find({"company_id": company, "dealer_id": b["dealer_id"], "bill_no": b.get("bill_no")}):
        lines.append({"model": s.get("model"), "group": s.get("group"), "brand": s.get("brand"),
                      "qty": s.get("qty"), "rate": s.get("rate"), "amount": s.get("amount"), "imei": s.get("imei")})
    return {"bill_no": b.get("bill_no"), "date": b.get("date"), "amount": b.get("amount"),
            "source": b.get("source"), "lines": lines,
            "line_total": round(sum(l["amount"] or 0 for l in lines), 2)}


@router.delete("/{bid}")
async def delete_bill(bid: str, company=Depends(current_company), _=Depends(require_roles("admin"))):
    """Admin: delete a bill (from PDF, import, statement or manual). Sale-CSV bills also
    return their sold stock to inventory and drop the linked sale lines."""
    b = await db.bills.find_one({"_id": bid, "company_id": company})
    if not b:
        raise HTTPException(404, "Bill not found")
    await db.bills.delete_one({"_id": bid, "company_id": company})
    reverted = 0
    if b.get("source") == "sale_csv" and b.get("bill_no"):
        async for u in db.stock_units.find({"company_id": company, "sale_bill": b["bill_no"],
                                            "sale_dealer_id": b["dealer_id"]}):
            await db.stock_units.update_one(
                {"_id": u["_id"]},
                {"$set": {"status": "in_stock"},
                 "$unset": {"sale_bill": "", "sale_dealer_id": "", "sale_dealer_name": "",
                            "sale_rate": "", "sale_date": ""}})
            reverted += 1
        await db.sales.delete_many({"company_id": company, "dealer_id": b["dealer_id"], "bill_no": b["bill_no"]})
    return {"ok": True, "reverted_units": reverted}
