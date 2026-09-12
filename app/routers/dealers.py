import re
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from pymongo import ReturnDocument

from ..auth import get_current_user, is_staff, require_roles
from ..deps import current_company
from ..db import db
from ..ledger import compute, bill_breakdown
from ..models import BillIn, BulkBills, DealerIn, DealerPatch, MergeIn, SeedIn, VisitIn
from ..serializers import public_dealer

router = APIRouter(prefix="/dealers", tags=["dealers"])
staff_only = require_roles("admin", "manager")


async def _summary(dealer_id):
    bills = [b async for b in db.bills.find({"dealer_id": dealer_id})]
    pays = [p async for p in db.payments.find({"dealer_id": dealer_id})]
    return compute(bills, pays)


@router.get("")
async def list_dealers(company=Depends(current_company), user=Depends(get_current_user)):
    query = {"company_id": company} if is_staff(user) else {"company_id": company, "collector_id": user["_id"]}
    dealers = [d async for d in db.dealers.find(query)]
    ids = [d["_id"] for d in dealers]
    bills_by, pays_by = {}, {}
    async for b in db.bills.find({"dealer_id": {"$in": ids}}):
        bills_by.setdefault(b["dealer_id"], []).append(b)
    async for p in db.payments.find({"dealer_id": {"$in": ids}}):
        pays_by.setdefault(p["dealer_id"], []).append(p)
    today = date.today().isoformat()
    visited = set()
    async for v in db.visits.find({"user_id": user["_id"], "date": today}):
        visited.add(v["dealer_id"])
    return [public_dealer(d, compute(bills_by.get(d["_id"], []), pays_by.get(d["_id"], [])), d["_id"] in visited) for d in dealers]


@router.get("/duplicates")
async def dealer_duplicates(company=Depends(current_company), _=Depends(require_roles("admin"))):
    """Group dealers that look like the same shop — same normalized name or same phone."""
    dealers = [d async for d in db.dealers.find({"company_id": company})]
    ids = [d["_id"] for d in dealers]
    bills_by, pays_by = {}, {}
    async for b in db.bills.find({"dealer_id": {"$in": ids}}):
        bills_by.setdefault(b["dealer_id"], []).append(b)
    async for p in db.payments.find({"dealer_id": {"$in": ids}}):
        pays_by.setdefault(p["dealer_id"], []).append(p)

    def norm(n):
        return re.sub(r"[^a-z0-9]", "", (n or "").lower())

    def ph(p):
        digits = "".join(c for c in (p or "") if c.isdigit())
        return digits[-10:] if len(digits) >= 10 else ""

    groups = {}
    for d in dealers:
        summ = compute(bills_by.get(d["_id"], []), pays_by.get(d["_id"], []))
        info = {"id": d["_id"], "name": d["name"], "phone": d.get("phone"),
                "outstanding": summ["outstanding"], "bills": len(bills_by.get(d["_id"], [])), "source": d.get("source")}
        nk = norm(d["name"])
        if nk:
            groups.setdefault(("name", nk), []).append(info)
        pk = ph(d.get("phone"))
        if pk:
            groups.setdefault(("phone", pk), []).append(info)
    out, seen = [], set()
    for (reason, key), members in groups.items():
        uniq = {m["id"]: m for m in members}
        if len(uniq) < 2:
            continue
        sig = tuple(sorted(uniq.keys()))
        if sig in seen:
            continue
        seen.add(sig)
        out.append({"key": reason + ":" + key, "reason": reason, "dealers": list(uniq.values())})
    return out


@router.get("/{did}")
async def get_dealer(did: str, company=Depends(current_company), user=Depends(get_current_user)):
    d = await db.dealers.find_one({"_id": did, "company_id": company})
    if not d:
        raise HTTPException(404, "Dealer not found")
    if not is_staff(user) and d.get("collector_id") != user["_id"]:
        raise HTTPException(403, "Not your dealer")
    today = date.today().isoformat()
    v = await db.visits.find_one({"user_id": user["_id"], "dealer_id": did, "date": today})
    return public_dealer(d, await _summary(did), bool(v))


@router.get("/{did}/ledger")
async def dealer_ledger(did: str, company=Depends(current_company), user=Depends(get_current_user)):
    d = await db.dealers.find_one({"_id": did, "company_id": company})
    if not d:
        raise HTTPException(404, "Dealer not found")
    if not is_staff(user) and d.get("collector_id") != user["_id"]:
        raise HTTPException(403, "Not your dealer")
    bills = [b async for b in db.bills.find({"dealer_id": did})]
    pays = [p async for p in db.payments.find({"dealer_id": did})]
    # per-bill ageing only for bills that still have an unpaid portion (FIFO, oldest cleared first)
    unpaid_age = {}
    for u in bill_breakdown(bills, pays):
        unpaid_age[(u["bill_no"], u["date"])] = {"days": u["days"], "bucket": u["bucket"]}
    rows = []
    for b in bills:
        info = unpaid_age.get((b.get("bill_no"), b.get("date")))
        rows.append({"id": b["_id"], "source": b.get("source"), "date": b.get("date"), "type": "bill",
                     "ref": b.get("bill_no"), "debit": b["amount"], "credit": 0,
                     "days": info["days"] if info else None, "bucket": info["bucket"] if info else None})
    for p in pays:
        if p.get("status") == "bounced" or not p.get("approved", True):
            continue
        rows.append({"id": p["_id"], "date": p.get("date"), "type": "payment", "ref": p.get("cheque") or p.get("mode"),
                     "debit": 0, "credit": p["amount"], "mode": p.get("mode")})
    rows.sort(key=lambda r: (r["date"] or "0000-00-00"))
    bal = 0
    for r in rows:
        bal += r["debit"] - r["credit"]
        r["balance"] = round(bal)
    summ = compute(bills, pays)
    return {"dealer": d["name"], "outstanding": summ["outstanding"], "ageing": summ["ageing"],
            "last_payment": summ["last_payment"], "credit_limit": d.get("credit_limit", 0), "entries": rows}


@router.post("")
async def create_dealer(body: DealerIn, opening_balance: float = 0, company=Depends(current_company), _=Depends(staff_only)):
    did = uuid.uuid4().hex
    d = {"_id": did, "name": body.name.strip(), "area": body.area, "phone": body.phone,
         "credit_limit": body.credit_limit, "collector_id": body.collector_id, "show_on_overview": body.show_on_overview, "company_id": company}
    await db.dealers.insert_one(d)
    if opening_balance and opening_balance > 0:
        await db.bills.insert_one({"_id": uuid.uuid4().hex, "dealer_id": did, "bill_no": "Opening",
                                   "date": date.today().isoformat(), "amount": opening_balance, "source": "opening", "company_id": company})
    return public_dealer(d, await _summary(did))


@router.patch("/{did}")
async def update_dealer(did: str, body: DealerPatch, company=Depends(current_company), _=Depends(staff_only)):
    upd = {}
    for f in ("name", "area", "phone", "credit_limit", "collector_id", "show_on_overview"):
        v = getattr(body, f)
        if v is not None:
            upd[f] = v.strip() if f == "name" else v
    if not upd:
        raise HTTPException(400, "Nothing to update")
    d = await db.dealers.find_one_and_update({"_id": did, "company_id": company}, {"$set": upd}, return_document=ReturnDocument.AFTER)
    if not d:
        raise HTTPException(404, "Dealer not found")
    return public_dealer(d, await _summary(did))


@router.delete("/{did}")
async def delete_dealer(did: str, company=Depends(current_company), _=Depends(require_roles("admin"))):
    if not await db.dealers.find_one({"_id": did, "company_id": company}):
        raise HTTPException(404, "Dealer not found")
    await db.bills.delete_many({"dealer_id": did})
    await db.payments.delete_many({"dealer_id": did})
    await db.dealers.delete_one({"_id": did})
    return {"ok": True}


@router.post("/{did}/bills")
async def add_bill(did: str, body: BillIn, company=Depends(current_company), _=Depends(staff_only)):
    if not await db.dealers.find_one({"_id": did, "company_id": company}):
        raise HTTPException(404, "Dealer not found")
    dupe = await db.bills.find_one({"dealer_id": did, "company_id": company,
                                    "bill_no": {"$regex": f"^{re.escape(body.bill_no.strip())}$", "$options": "i"}})
    if dupe:
        raise HTTPException(409, f"Bill {body.bill_no.strip()} already exists for this dealer")
    await db.bills.insert_one({"_id": uuid.uuid4().hex, "dealer_id": did, "bill_no": body.bill_no.strip(),
                               "date": body.date, "amount": body.amount, "source": (body.source or "manual"), "company_id": company})
    return {"ok": True, "summary": await _summary(did)}


@router.post("/{did}/seed")
async def seed_ledger(did: str, body: SeedIn, company=Depends(current_company), user=Depends(get_current_user)):
    """Seed a dealer's ledger from an imported statement. Admin: anytime.
    Manager (if allowed): only once, while the ledger is still empty."""
    if not await db.dealers.find_one({"_id": did, "company_id": company}):
        raise HTTPException(404, "Dealer not found")
    if user["role"] != "admin":
        if user["role"] != "manager" or not user.get("can_import_statement"):
            raise HTTPException(403, "You are not allowed to import statements")
        if await db.bills.count_documents({"dealer_id": did, "company_id": company}) > 0:
            raise HTTPException(403, "Statement already imported for this dealer — ask an admin to re-import")
    await db.bills.delete_many({"dealer_id": did})
    await db.payments.delete_many({"dealer_id": did})
    docs = []
    if body.opening and body.opening > 0:
        docs.append({"_id": uuid.uuid4().hex, "dealer_id": did, "bill_no": "Opening",
                     "date": body.opening_date or "2000-01-01", "amount": body.opening, "source": "opening", "company_id": company})
    for b in body.bills:
        docs.append({"_id": uuid.uuid4().hex, "dealer_id": did, "bill_no": b.bill_no,
                     "date": b.date, "amount": b.amount, "source": "statement", "company_id": company})
    if docs:
        await db.bills.insert_many(docs)
    pdocs = []
    for p in body.payments:
        pdocs.append({"_id": uuid.uuid4().hex, "dealer_id": did, "collector_id": "seed", "collector_name": "Statement",
                      "amount": p.amount, "mode": "Cheque", "cheque": p.ref or "", "date": p.date,
                      "receipt": None, "status": "cleared", "deposited": True, "approved": True, "company_id": company})
    if pdocs:
        await db.payments.insert_many(pdocs)
    return {"ok": True, "summary": await _summary(did)}


@router.post("/{did}/merge")
async def merge_dealers(did: str, body: MergeIn, company=Depends(current_company), _=Depends(require_roles("admin"))):
    """Move all bills, payments, sales and stock links from source dealers into the target, then delete the sources."""
    target = await db.dealers.find_one({"_id": did, "company_id": company})
    if not target:
        raise HTTPException(404, "Target dealer not found")
    merged = 0
    for sid in body.source_ids:
        if sid == did:
            continue
        src = await db.dealers.find_one({"_id": sid, "company_id": company})
        if not src:
            continue
        await db.bills.update_many({"dealer_id": sid}, {"$set": {"dealer_id": did}})
        await db.payments.update_many({"dealer_id": sid}, {"$set": {"dealer_id": did}})
        await db.sales.update_many({"company_id": company, "dealer_id": sid}, {"$set": {"dealer_id": did}})
        await db.stock_units.update_many({"company_id": company, "sale_dealer_id": sid}, {"$set": {"sale_dealer_id": did}})
        if not target.get("phone") and src.get("phone"):
            await db.dealers.update_one({"_id": did}, {"$set": {"phone": src["phone"]}})
            target["phone"] = src["phone"]
        await db.dealers.delete_one({"_id": sid, "company_id": company})
        merged += 1
    return {"ok": True, "merged": merged, "summary": await _summary(did)}


@router.post("/{did}/visit")
async def mark_visited(did: str, body: VisitIn = Body(default=VisitIn()), company=Depends(current_company), user=Depends(get_current_user)):
    d = await db.dealers.find_one({"_id": did, "company_id": company})
    if not d:
        raise HTTPException(404, "Dealer not found")
    if not is_staff(user) and d.get("collector_id") != user["_id"]:
        raise HTTPException(403, "Not your dealer")
    today = date.today().isoformat()
    now = datetime.now(timezone.utc)
    insert = {"_id": uuid.uuid4().hex, "dealer_id": did, "dealer_name": d["name"], "company_id": company,
              "user_id": user["_id"], "user_name": user["name"], "role": user["role"],
              "date": today, "first_ts": now}
    if body and body.lat is not None and body.lng is not None:
        insert["lat"] = body.lat
        insert["lng"] = body.lng
    await db.visits.update_one(
        {"dealer_id": did, "user_id": user["_id"], "date": today},
        {"$setOnInsert": insert, "$set": {"last_ts": now}},
        upsert=True)
    return {"ok": True}
