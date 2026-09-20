import uuid
from datetime import datetime, timezone, date
from typing import Optional, List

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from ..auth import get_current_user, hash_pin
from ..deps import current_company
from ..db import db

router = APIRouter(prefix="/dc", tags=["daily-collections"])

CASH_MODES = ("cash", "card", "upi", "finance")


async def dc_company(company=Depends(current_company)):
    c = await db.companies.find_one({"_id": company})
    if not c or c.get("kind") != "daily_collections":
        raise HTTPException(400, "Not a Daily Collections company")
    return company


def _today():
    return date.today().isoformat()


def _is_admin(u):
    return u.get("role") == "admin"


def _sees_all(u):
    # Admin and managers (admin-lite) see everyone's data; employees see only their own.
    return u.get("role") in ("admin", "manager")


class ExchangeItem(BaseModel):
    brand: Optional[str] = ""
    model: Optional[str] = ""
    note: Optional[str] = ""
    value: float = 0


class BillItem(BaseModel):
    brand: Optional[str] = ""
    category: Optional[str] = ""
    model: Optional[str] = ""
    qty: float = 1


class BillIn(BaseModel):
    date: Optional[str] = None
    bill_no: Optional[str] = ""
    customer: Optional[str] = ""
    phone: Optional[str] = ""
    items: List[BillItem] = []
    exchange: Optional[ExchangeItem] = None
    total: float = 0
    cash: float = 0
    card: float = 0
    upi: float = 0
    finance: float = 0
    nlc: float = 0
    pending: float = 0
    note: Optional[str] = ""


class BillPatch(BaseModel):
    bill_no: Optional[str] = None
    items: Optional[List[BillItem]] = None
    customer: Optional[str] = None
    phone: Optional[str] = None
    total: Optional[float] = None
    cash: Optional[float] = None
    card: Optional[float] = None
    upi: Optional[float] = None
    finance: Optional[float] = None
    nlc: Optional[float] = None
    pending: Optional[float] = None
    note: Optional[str] = None


class CollectIn(BaseModel):
    amount: float
    mode: Optional[str] = "cash"


class ExpenseIn(BaseModel):
    date: Optional[str] = None
    category: Optional[str] = ""
    amount: float = 0
    paid_by: Optional[str] = ""
    note: Optional[str] = ""


class ReconIn(BaseModel):
    date: str
    float_open: float = 0
    handover: float = 0
    note: Optional[str] = ""


class ResaleIn(BaseModel):
    amount: float
    mode: Optional[str] = "cash"
    note: Optional[str] = ""


class HandoverIn(BaseModel):
    staff_id: str
    amount: float
    note: Optional[str] = ""


class ApproveIn(BaseModel):
    approve: bool = True


class CheckIn(BaseModel):
    checked: bool = True


class DcUserIn(BaseModel):
    name: str
    pin: str
    role: str = "collector"
    perms: dict = {}


class DcUserPatch(BaseModel):
    name: Optional[str] = None
    pin: Optional[str] = None
    role: Optional[str] = None
    perms: Optional[dict] = None


EXPENSE_APPROVAL_LIMIT = 1000  # employee expenses above this need admin approval


EDIT_WINDOW_SEC = 120


def _editable_until(b):
    try:
        return datetime.fromisoformat(b["created_at"]).timestamp() + EDIT_WINDOW_SEC
    except Exception:
        return 0


def _pub_bill(b, admin, resale=0):
    until = _editable_until(b)
    out = {
        "editable_until": until, "locked": datetime.now(timezone.utc).timestamp() > until,
        "id": b["_id"], "date": b.get("date"), "bill_no": b.get("bill_no", ""),
        "customer": b.get("customer", ""), "phone": b.get("phone", ""),
        "items": b.get("items", []), "total": b.get("total", 0),
        "cash": b.get("cash", 0), "card": b.get("card", 0), "upi": b.get("upi", 0),
        "finance": b.get("finance", 0),
        "pending": b.get("pending", 0), "note": b.get("note", ""),
        "staff": b.get("staff_name", ""), "staff_id": b.get("staff_id"),
        "exchange": b.get("exchange") or None, "resale": round(resale or 0),
        "created_at": b.get("created_at"),
    }
    if admin:
        out["nlc"] = b.get("nlc", 0)
        out["profit"] = round((b.get("total", 0) or 0) - (b.get("nlc", 0) or 0) + (resale or 0))
    return out


async def _resale_by_bill(company, bill_ids):
    m = {}
    ids = list(bill_ids)
    if not ids:
        return m
    async for x in db.dc_exchanges.find({"company_id": company, "bill_id": {"$in": ids}, "status": "resold"}):
        m[x["bill_id"]] = m.get(x["bill_id"], 0) + (x.get("resale_amount", 0) or 0)
    return m


# ---------------- Bills ----------------
@router.get("/bills")
async def list_bills(date: Optional[str] = None, frm: Optional[str] = None, to: Optional[str] = None,
                     company=Depends(dc_company), user=Depends(get_current_user)):
    q = {"company_id": company}
    if date:
        q["date"] = date
    elif frm and to:
        q["date"] = {"$gte": frm, "$lte": to}
    if not _sees_all(user):
        q["staff_id"] = user["_id"]
    admin = _is_admin(user)
    docs = [b async for b in db.dc_bills.find(q).sort("created_at", -1)]
    resale = await _resale_by_bill(company, {d["_id"] for d in docs})
    rows = [_pub_bill(b, admin, resale.get(b["_id"], 0)) for b in docs]
    tot = {"total": sum(r["total"] for r in rows), "cash": sum(r["cash"] for r in rows),
           "card": sum(r["card"] for r in rows), "upi": sum(r["upi"] for r in rows),
           "finance": sum(r["finance"] for r in rows),
           "pending": sum(r["pending"] for r in rows), "bills": len(rows)}
    if admin:
        tot["nlc"] = sum(r.get("nlc", 0) for r in rows)
        tot["resale"] = sum(r.get("resale", 0) for r in rows)
        tot["profit"] = round(tot["total"] - tot["nlc"] + tot["resale"])
    return {"rows": rows, "totals": tot, "is_admin": admin}


@router.post("/bills")
async def create_bill(body: BillIn, company=Depends(dc_company), user=Depends(get_current_user)):
    doc = body.model_dump()
    exchange = doc.pop("exchange", None)
    doc["items"] = [i for i in doc.get("items", [])]
    bid = uuid.uuid4().hex
    doc.update({"_id": bid, "company_id": company, "date": body.date or _today(),
                "staff_id": user["_id"], "staff_name": user.get("name", ""),
                "created_at": datetime.now(timezone.utc).isoformat()})
    if exchange and (exchange.get("model") or exchange.get("brand")):
        doc["exchange"] = {"brand": exchange.get("brand", ""), "model": exchange.get("model", ""),
                           "note": exchange.get("note", ""), "value": exchange.get("value", 0) or 0}
        await db.dc_exchanges.insert_one({
            "_id": uuid.uuid4().hex, "company_id": company, "bill_id": bid,
            "seller_staff_id": user["_id"], "seller_staff_name": user.get("name", ""),
            "customer": body.customer or "", "brand": exchange.get("brand", ""),
            "model": exchange.get("model", ""), "note": exchange.get("note", ""),
            "value": exchange.get("value", 0) or 0, "status": "in_stock",
            "resale_amount": 0, "resale_mode": "", "resold_date": None,
            "date": doc["date"], "created_at": doc["created_at"],
        })
    await db.dc_bills.insert_one(doc)
    return _pub_bill(doc, _is_admin(user))


@router.patch("/bills/{bid}")
async def update_bill(bid: str, body: BillPatch, company=Depends(dc_company), user=Depends(get_current_user)):
    upd = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not upd:
        raise HTTPException(400, "Nothing to update")
    b = await db.dc_bills.find_one({"_id": bid, "company_id": company})
    if not b:
        raise HTTPException(404, "Bill not found")
    if datetime.now(timezone.utc).timestamp() > _editable_until(b):
        raise HTTPException(403, "Bill is locked — edits are allowed only within 2 minutes of saving")
    if "items" in upd:
        upd["items"] = [dict(i) for i in upd["items"]]
    await db.dc_bills.update_one({"_id": bid}, {"$set": upd})
    b.update(upd)
    return _pub_bill(b, _is_admin(user))


@router.post("/bills/{bid}/collect")
async def collect_pending(bid: str, body: CollectIn, company=Depends(dc_company), user=Depends(get_current_user)):
    b = await db.dc_bills.find_one({"_id": bid, "company_id": company})
    if not b:
        raise HTTPException(404, "Bill not found")
    amt = max(0, float(body.amount))
    new_pending = max(0, (b.get("pending", 0) or 0) - amt)
    mode = (body.mode or "cash").lower()
    if mode not in CASH_MODES:
        mode = "cash"
    await db.dc_bills.update_one({"_id": bid}, {"$set": {"pending": new_pending}})
    await db.dc_receipts.insert_one({
        "_id": uuid.uuid4().hex, "company_id": company, "bill_id": bid,
        "staff_id": user["_id"], "staff_name": user.get("name", ""),
        "amount": amt, "mode": mode, "date": _today(), "bill_no": b.get("bill_no", ""),
        "customer": b.get("customer", ""), "checked": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    b["pending"] = new_pending
    return _pub_bill(b, _is_admin(user))


@router.get("/calendar")
async def dc_calendar(month: str, company=Depends(dc_company), user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    q = {"company_id": company, "date": {"$gte": month + "-01", "$lte": month + "-31"}}
    days = {}
    async for b in db.dc_bills.find(q):
        d = days.setdefault(b["date"], {"date": b["date"], "total": 0, "bills": 0, "nlc": 0, "pending": 0, "resale": 0})
        d["total"] += b.get("total", 0) or 0
        d["bills"] += 1
        d["nlc"] += b.get("nlc", 0) or 0
        d["pending"] += b.get("pending", 0) or 0
    async for x in db.dc_exchanges.find({"company_id": company, "status": "resold",
                                         "resold_date": {"$gte": month + "-01", "$lte": month + "-31"}}):
        d = days.setdefault(x["resold_date"], {"date": x["resold_date"], "total": 0, "bills": 0, "nlc": 0, "pending": 0, "resale": 0})
        d["resale"] += x.get("resale_amount", 0) or 0
    async for e in db.dc_expenses.find(q):
        d = days.setdefault(e["date"], {"date": e["date"], "total": 0, "bills": 0, "nlc": 0, "pending": 0, "resale": 0})
        d["expenses"] = d.get("expenses", 0) + (e.get("amount", 0) or 0)
    rows = []
    for d in sorted(days.values(), key=lambda x: x["date"]):
        d["profit"] = round(d["total"] - d.pop("nlc") + d.pop("resale"))
        d["total"] = round(d["total"]); d["pending"] = round(d["pending"]); d["expenses"] = round(d.get("expenses", 0))
        rows.append(d)
    tot = round(sum(r["total"] for r in rows))
    return {"month": month, "days": rows, "total": tot, "bills": sum(r["bills"] for r in rows),
            "profit": round(sum(r["profit"] for r in rows)), "best": max(rows, key=lambda r: r["total"])["date"] if rows else None}


@router.delete("/bills/{bid}")
async def delete_bill(bid: str, company=Depends(dc_company), _=Depends(get_current_user)):
    await db.dc_bills.delete_one({"_id": bid, "company_id": company})
    await db.dc_exchanges.delete_many({"bill_id": bid, "company_id": company})
    await db.dc_receipts.delete_many({"bill_id": bid, "company_id": company})
    return {"ok": True}


@router.get("/pending")
async def pending(frm: Optional[str] = None, to: Optional[str] = None, mine: bool = False,
                  company=Depends(dc_company), user=Depends(get_current_user)):
    q = {"company_id": company, "pending": {"$gt": 0}}
    if frm and to:
        q["date"] = {"$gte": frm, "$lte": to}
    if mine or not _sees_all(user):
        q["staff_id"] = user["_id"]
    admin = _is_admin(user)
    rows = [_pub_bill(b, admin) async for b in db.dc_bills.find(q).sort("date", 1)]
    return {"rows": rows, "total": sum(r["pending"] for r in rows), "count": len(rows)}


# ---------------- Exchange / trade-in ----------------
def _pub_exchange(x):
    return {"id": x["_id"], "bill_id": x.get("bill_id"), "brand": x.get("brand", ""),
            "model": x.get("model", ""), "note": x.get("note", ""), "value": x.get("value", 0),
            "customer": x.get("customer", ""), "status": x.get("status", "in_stock"),
            "resale_amount": x.get("resale_amount", 0), "resale_mode": x.get("resale_mode", ""),
            "resale_status": x.get("resale_status", "none"),
            "pending_amount": x.get("pending_amount", 0), "pending_mode": x.get("pending_mode", ""),
            "resold_date": x.get("resold_date"), "staff": x.get("seller_staff_name", ""),
            "staff_id": x.get("seller_staff_id"), "date": x.get("date")}


@router.get("/exchanges")
async def list_exchanges(status: Optional[str] = None, company=Depends(dc_company), user=Depends(get_current_user)):
    q = {"company_id": company}
    if status in ("in_stock", "resold", "godown"):
        q["status"] = status
    if not _sees_all(user):
        q["seller_staff_id"] = user["_id"]
    rows = [_pub_exchange(x) async for x in db.dc_exchanges.find(q).sort("created_at", -1)]
    summ = {"in_stock": 0, "resold": 0, "godown": 0, "resale_total": 0}
    for r in rows:
        summ[r["status"]] = summ.get(r["status"], 0) + 1
        if r["status"] == "resold":
            summ["resale_total"] += r["resale_amount"] or 0
    return {"rows": rows, "summary": summ}


@router.post("/exchanges/{xid}/resale")
async def resale_exchange(xid: str, body: ResaleIn, company=Depends(dc_company), user=Depends(get_current_user)):
    x = await db.dc_exchanges.find_one({"_id": xid, "company_id": company})
    if not x:
        raise HTTPException(404, "Exchange item not found")
    mode = (body.mode or "cash").lower()
    if mode not in CASH_MODES:
        mode = "cash"
    amt = max(0, float(body.amount))
    if _is_admin(user):
        await db.dc_exchanges.update_one({"_id": xid}, {"$set": {
            "status": "resold", "resale_amount": amt, "resale_mode": mode,
            "resold_date": _today(), "resale_note": body.note or "", "resale_status": "approved"},
            "$unset": {"pending_amount": "", "pending_mode": "", "pending_by": "", "pending_at": ""}})
        return {"ok": True, "status": "resold"}
    await db.dc_exchanges.update_one({"_id": xid}, {"$set": {
        "resale_status": "pending", "pending_amount": amt, "pending_mode": mode,
        "pending_by": user["_id"], "pending_by_name": user.get("name", ""),
        "pending_at": datetime.now(timezone.utc).isoformat(), "resale_note": body.note or ""}})
    return {"ok": True, "status": "pending"}


@router.post("/exchanges/{xid}/godown")
async def godown_exchange(xid: str, company=Depends(dc_company), _=Depends(get_current_user)):
    x = await db.dc_exchanges.find_one({"_id": xid, "company_id": company})
    if not x:
        raise HTTPException(404, "Exchange item not found")
    await db.dc_exchanges.update_one({"_id": xid}, {"$set": {"status": "godown"}})
    return {"ok": True}


@router.delete("/exchanges/{xid}")
async def delete_exchange(xid: str, company=Depends(dc_company), _=Depends(get_current_user)):
    await db.dc_exchanges.delete_one({"_id": xid, "company_id": company})
    return {"ok": True}


# ---------------- Cash in hand (rebuilt from records) ----------------
async def _cash_components(company, staff_ids):
    comp = {sid: {"bills_cash": 0.0, "receipts_cash": 0.0, "resale_cash": 0.0, "expenses": 0.0, "handover": 0.0}
            for sid in staff_ids}
    async for b in db.dc_bills.find({"company_id": company}):
        sid = b.get("staff_id")
        if sid in comp:
            comp[sid]["bills_cash"] += b.get("cash", 0) or 0
    async for r in db.dc_receipts.find({"company_id": company, "mode": "cash"}):
        sid = r.get("staff_id")
        if sid in comp:
            comp[sid]["receipts_cash"] += r.get("amount", 0) or 0
    async for x in db.dc_exchanges.find({"company_id": company, "status": "resold", "resale_mode": "cash"}):
        sid = x.get("seller_staff_id")
        if sid in comp:
            comp[sid]["resale_cash"] += x.get("resale_amount", 0) or 0
    async for e in db.dc_expenses.find({"company_id": company, "status": {"$ne": "pending"}}):
        sid = e.get("staff_id")
        if sid in comp:
            comp[sid]["expenses"] += e.get("amount", 0) or 0
    async for h in db.dc_handovers.find({"company_id": company}):
        sid = h.get("staff_id")
        if sid in comp:
            comp[sid]["handover"] += h.get("amount", 0) or 0
    return comp


@router.get("/cash")
async def cash_in_hand(company=Depends(dc_company), user=Depends(get_current_user)):
    staff = {}
    if _sees_all(user):
        async for u in db.users.find({"company_ids": company}):
            staff[u["_id"]] = u.get("name", "")
        async for b in db.dc_bills.find({"company_id": company}, {"staff_id": 1, "staff_name": 1}):
            sid = b.get("staff_id")
            if sid and sid not in staff:
                staff[sid] = b.get("staff_name", "")
    else:
        staff[user["_id"]] = user.get("name", "")
    comp = await _cash_components(company, set(staff.keys()))
    rows = []
    for sid, name in staff.items():
        c = comp.get(sid, {"bills_cash": 0, "receipts_cash": 0, "resale_cash": 0, "expenses": 0, "handover": 0})
        cih = c["bills_cash"] + c["receipts_cash"] + c["resale_cash"] - c["expenses"] - c["handover"]
        rows.append({"staff_id": sid, "name": name, "cash_in_hand": round(cih),
                     "bills_cash": round(c["bills_cash"]), "receipts_cash": round(c["receipts_cash"]),
                     "resale_cash": round(c["resale_cash"]), "expenses": round(c["expenses"]),
                     "handover": round(c["handover"])})
    rows.sort(key=lambda r: -r["cash_in_hand"])
    return {"rows": rows, "total": round(sum(r["cash_in_hand"] for r in rows)),
            "is_admin": _is_admin(user), "sees_all": _sees_all(user)}


@router.post("/handover")
async def handover(body: HandoverIn, company=Depends(dc_company), user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(403, "Only the admin can receive cash handovers")
    if body.amount <= 0:
        raise HTTPException(400, "Enter an amount")
    u = await db.users.find_one({"_id": body.staff_id})
    await db.dc_handovers.insert_one({
        "_id": uuid.uuid4().hex, "company_id": company, "staff_id": body.staff_id,
        "staff_name": (u or {}).get("name", ""), "amount": float(body.amount), "note": body.note or "",
        "date": _today(), "by": user["_id"], "by_name": user.get("name", ""),
        "created_at": datetime.now(timezone.utc).isoformat()})
    return {"ok": True}


@router.get("/handovers")
async def list_handovers(staff_id: Optional[str] = None, company=Depends(dc_company), user=Depends(get_current_user)):
    q = {"company_id": company}
    if not _sees_all(user):
        q["staff_id"] = user["_id"]
    elif staff_id:
        q["staff_id"] = staff_id
    rows = []
    async for h in db.dc_handovers.find(q).sort("created_at", -1):
        rows.append({"id": h["_id"], "staff_id": h.get("staff_id"), "staff_name": h.get("staff_name", ""),
                     "amount": h.get("amount", 0), "note": h.get("note", ""), "date": h.get("date"),
                     "by_name": h.get("by_name", ""), "created_at": h.get("created_at")})
    return {"rows": rows, "total": round(sum(r["amount"] for r in rows))}


# ---------------- Verification queue (admin approves before it hits the numbers) ----------------
@router.get("/pending-approvals")
async def pending_approvals(company=Depends(dc_company), user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    expenses = []
    async for e in db.dc_expenses.find({"company_id": company, "status": "pending"}).sort("created_at", -1):
        expenses.append({"id": e["_id"], "date": e.get("date"), "category": e.get("category", ""),
                         "amount": e.get("amount", 0), "note": e.get("note", ""), "staff": e.get("staff_name", "")})
    resales = []
    async for x in db.dc_exchanges.find({"company_id": company, "resale_status": "pending"}).sort("pending_at", -1):
        resales.append({"id": x["_id"], "brand": x.get("brand", ""), "model": x.get("model", ""),
                        "amount": x.get("pending_amount", 0), "mode": x.get("pending_mode", ""),
                        "staff": x.get("seller_staff_name", ""), "customer": x.get("customer", "")})
    return {"expenses": expenses, "resales": resales, "count": len(expenses) + len(resales)}


@router.post("/approvals/expense/{eid}")
async def approve_expense(eid: str, body: ApproveIn, company=Depends(dc_company), user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    e = await db.dc_expenses.find_one({"_id": eid, "company_id": company})
    if not e:
        raise HTTPException(404, "Expense not found")
    if body.approve:
        await db.dc_expenses.update_one({"_id": eid}, {"$set": {"status": "approved"}})
    else:
        await db.dc_expenses.delete_one({"_id": eid})
    return {"ok": True}


@router.post("/approvals/resale/{xid}")
async def approve_resale(xid: str, body: ApproveIn, company=Depends(dc_company), user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    x = await db.dc_exchanges.find_one({"_id": xid, "company_id": company})
    if not x or x.get("resale_status") != "pending":
        raise HTTPException(404, "No pending resale for this item")
    if body.approve:
        await db.dc_exchanges.update_one({"_id": xid}, {"$set": {
            "status": "resold", "resale_amount": x.get("pending_amount", 0),
            "resale_mode": x.get("pending_mode", "cash"), "resold_date": _today(), "resale_status": "approved"},
            "$unset": {"pending_amount": "", "pending_mode": "", "pending_by": "", "pending_at": ""}})
    else:
        await db.dc_exchanges.update_one({"_id": xid}, {"$set": {"resale_status": "rejected"},
            "$unset": {"pending_amount": "", "pending_mode": "", "pending_by": "", "pending_at": ""}})
    return {"ok": True}


# ---------------- Reconciliation (tick off + missing bill-number detection) ----------------
@router.get("/reconcile")
async def reconcile(frm: str, to: str, company=Depends(dc_company), user=Depends(get_current_user)):
    if not _sees_all(user):
        raise HTTPException(403, "Not allowed for your role")
    bills, nums = [], []
    async for b in db.dc_bills.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}).sort("date", 1):
        bills.append({"id": b["_id"], "date": b.get("date"), "bill_no": b.get("bill_no", ""),
                      "customer": b.get("customer", ""), "total": b.get("total", 0),
                      "staff": b.get("staff_name", ""), "checked": bool(b.get("checked"))})
        bn = str(b.get("bill_no", "")).strip()
        if bn.isdigit():
            nums.append(int(bn))
    receipts = []
    async for r in db.dc_receipts.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}).sort("created_at", -1):
        receipts.append({"id": r["_id"], "date": r.get("date"), "amount": r.get("amount", 0),
                         "mode": r.get("mode", ""), "customer": r.get("customer", ""),
                         "bill_no": r.get("bill_no", ""), "staff": r.get("staff_name", ""),
                         "checked": bool(r.get("checked"))})
    missing = []
    if nums:
        present = set(nums)
        missing = [n for n in range(min(nums), max(nums) + 1) if n not in present]
    return {"bills": bills, "receipts": receipts, "missing_bill_nos": missing[:300],
            "summary": {"bills": len(bills), "bills_checked": sum(1 for b in bills if b["checked"]),
                        "receipts": len(receipts), "receipts_checked": sum(1 for r in receipts if r["checked"]),
                        "missing": len(missing)}}


@router.patch("/bills/{bid}/check")
async def check_bill(bid: str, body: CheckIn, company=Depends(dc_company), user=Depends(get_current_user)):
    if not _sees_all(user):
        raise HTTPException(403, "Not allowed for your role")
    r = await db.dc_bills.find_one_and_update({"_id": bid, "company_id": company}, {"$set": {"checked": bool(body.checked)}})
    if not r:
        raise HTTPException(404, "Bill not found")
    return {"ok": True}


@router.patch("/receipts/{rid}/check")
async def check_receipt(rid: str, body: CheckIn, company=Depends(dc_company), user=Depends(get_current_user)):
    if not _sees_all(user):
        raise HTTPException(403, "Not allowed for your role")
    r = await db.dc_receipts.find_one_and_update({"_id": rid, "company_id": company}, {"$set": {"checked": bool(body.checked)}})
    if not r:
        raise HTTPException(404, "Receipt not found")
    return {"ok": True}


# ---------------- DC-only user management (admin) + per-feature permissions ----------------
def _pub_dc_user(u):
    return {"id": u["_id"], "name": u.get("name", ""), "role": u.get("role", "collector"),
            "perms": u.get("dc_perms") or {}}


@router.get("/my-perms")
async def my_perms(company=Depends(dc_company), user=Depends(get_current_user)):
    return {"role": user.get("role"), "is_admin": _is_admin(user), "perms": user.get("dc_perms") or {}}


@router.get("/users")
async def dc_list_users(company=Depends(dc_company), user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    return [_pub_dc_user(u) async for u in db.users.find({"company_ids": company})]


@router.post("/users")
async def dc_create_user(body: DcUserIn, company=Depends(dc_company), user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    if not body.name.strip():
        raise HTTPException(400, "Name is required")
    if len(body.pin or "") != 4:
        raise HTTPException(400, "Set a 4-digit PIN")
    if await db.users.find_one({"name": body.name.strip()}):
        raise HTTPException(400, "A user with that name already exists")
    role = body.role if body.role in ("collector", "manager", "admin") else "collector"
    uid = uuid.uuid4().hex
    u = {"_id": uid, "name": body.name.strip(), "pin_hash": hash_pin(body.pin), "role": role,
         "company_ids": [company], "dc_perms": body.perms or {}}
    await db.users.insert_one(u)
    return _pub_dc_user(u)


@router.patch("/users/{uid}")
async def dc_update_user(uid: str, body: DcUserPatch, company=Depends(dc_company), user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    u = await db.users.find_one({"_id": uid, "company_ids": company})
    if not u:
        raise HTTPException(404, "User not found")
    upd = {}
    if body.name is not None:
        upd["name"] = body.name.strip()
    if body.role in ("collector", "manager", "admin"):
        upd["role"] = body.role
    if body.pin:
        if len(body.pin) != 4:
            raise HTTPException(400, "PIN must be 4 digits")
        upd["pin_hash"] = hash_pin(body.pin)
    if body.perms is not None:
        upd["dc_perms"] = body.perms
    if not upd:
        raise HTTPException(400, "Nothing to update")
    await db.users.update_one({"_id": uid}, {"$set": upd})
    u.update(upd)
    return _pub_dc_user(u)


@router.delete("/users/{uid}")
async def dc_delete_user(uid: str, company=Depends(dc_company), user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    if uid == user["_id"]:
        raise HTTPException(400, "You cannot remove your own account")
    await db.users.update_one({"_id": uid}, {"$pull": {"company_ids": company}})
    nu = await db.users.find_one({"_id": uid})
    if nu and not (nu.get("company_ids") or []) and nu.get("role") != "admin":
        await db.users.delete_one({"_id": uid})
    return {"ok": True}


# ---------------- Expenses ----------------
@router.get("/expenses")
async def list_expenses(date: Optional[str] = None, month: Optional[str] = None,
                        company=Depends(dc_company), user=Depends(get_current_user)):
    q = {"company_id": company}
    if date:
        q["date"] = date
    elif month:
        q["date"] = {"$regex": "^" + month}
    if not _sees_all(user):
        q["staff_id"] = user["_id"]
    rows = []
    async for e in db.dc_expenses.find(q).sort("created_at", -1):
        rows.append({"id": e["_id"], "date": e.get("date"), "category": e.get("category", ""),
                     "amount": e.get("amount", 0), "paid_by": e.get("paid_by", ""),
                     "note": e.get("note", ""), "staff": e.get("staff_name", ""),
                     "status": e.get("status", "approved")})
    return {"rows": rows, "total": round(sum(r["amount"] for r in rows if r["status"] != "pending")),
            "pending_total": round(sum(r["amount"] for r in rows if r["status"] == "pending"))}


@router.post("/expenses")
async def create_expense(body: ExpenseIn, company=Depends(dc_company), user=Depends(get_current_user)):
    eid = uuid.uuid4().hex
    doc = body.model_dump()
    approved = _is_admin(user) or (body.amount or 0) <= EXPENSE_APPROVAL_LIMIT
    doc.update({"_id": eid, "company_id": company, "date": body.date or _today(),
                "staff_id": user["_id"], "staff_name": user.get("name", ""),
                "status": "approved" if approved else "pending",
                "created_at": datetime.now(timezone.utc).isoformat()})
    await db.dc_expenses.insert_one(doc)
    return {"id": eid, "date": doc["date"], "category": doc["category"], "amount": doc["amount"],
            "paid_by": doc["paid_by"], "note": doc["note"], "staff": doc["staff_name"], "status": doc["status"]}


@router.delete("/expenses/{eid}")
async def delete_expense(eid: str, company=Depends(dc_company), _=Depends(get_current_user)):
    await db.dc_expenses.delete_one({"_id": eid, "company_id": company})
    return {"ok": True}


# ---------------- Day close / reconciliation ----------------
@router.get("/day")
async def day_summary(date: Optional[str] = None, company=Depends(dc_company), user=Depends(get_current_user)):
    d = date or _today()
    admin = _is_admin(user)
    bq = {"company_id": company, "date": d}
    if not _sees_all(user):
        bq["staff_id"] = user["_id"]
    bills = [b async for b in db.dc_bills.find(bq)]
    eq = dict(bq)
    eq["status"] = {"$ne": "pending"}
    exp = [e async for e in db.dc_expenses.find(eq)]
    receipts = [r async for r in db.dc_receipts.find(bq)]
    cash_bills = sum(b.get("cash", 0) or 0 for b in bills)
    cash_recv = sum(r.get("amount", 0) or 0 for r in receipts if r.get("mode") == "cash")
    cash_in = cash_bills + cash_recv
    total = sum(b.get("total", 0) or 0 for b in bills)
    pend = sum(b.get("pending", 0) or 0 for b in bills)
    exp_total = sum(e.get("amount", 0) or 0 for e in exp)
    r = await db.dc_recon.find_one({"company_id": company, "date": d}) or {}
    float_open = r.get("float_open", 0) or 0
    handover = r.get("handover", 0) or 0
    expected = float_open + cash_in - exp_total
    out = {
        "date": d, "bills": len(bills), "total": round(total), "cash_in": round(cash_in),
        "cash_receipts": round(cash_recv),
        "card": round(sum(b.get("card", 0) or 0 for b in bills)),
        "upi": round(sum(b.get("upi", 0) or 0 for b in bills)),
        "finance": round(sum(b.get("finance", 0) or 0 for b in bills)),
        "pending": round(pend), "expenses": round(exp_total),
        "float_open": round(float_open), "handover": round(handover),
        "expected_in_drawer": round(expected), "diff": round(expected - handover),
        "note": r.get("note", ""),
    }
    if admin:
        nlc = sum(b.get("nlc", 0) or 0 for b in bills)
        resale = 0
        async for x in db.dc_exchanges.find({"company_id": company, "status": "resold", "resold_date": d}):
            resale += x.get("resale_amount", 0) or 0
        out["nlc"] = round(nlc)
        out["resale"] = round(resale)
        out["profit"] = round(total - nlc + resale)
    return out


@router.put("/recon")
async def set_recon(body: ReconIn, company=Depends(dc_company), _=Depends(get_current_user)):
    await db.dc_recon.update_one(
        {"company_id": company, "date": body.date},
        {"$set": {"float_open": body.float_open, "handover": body.handover, "note": body.note or ""}},
        upsert=True)
    return {"ok": True}


# ---------------- Reports ----------------
@router.get("/reports")
async def dc_reports(frm: str, to: str, company=Depends(dc_company), user=Depends(get_current_user)):
    admin = _is_admin(user)
    seesall = _sees_all(user)
    by_day, by_staff, by_pay = {}, {}, {"cash": 0, "card": 0, "upi": 0, "finance": 0}
    tot = {"total": 0.0, "pending": 0.0, "nlc": 0.0, "bills": 0, "resale": 0.0}
    bq = {"company_id": company, "date": {"$gte": frm, "$lte": to}}
    if not seesall:
        bq["staff_id"] = user["_id"]
    async for b in db.dc_bills.find(bq):
        t = b.get("total", 0) or 0
        tot["total"] += t
        tot["pending"] += b.get("pending", 0) or 0
        tot["nlc"] += b.get("nlc", 0) or 0
        tot["bills"] += 1
        for k in by_pay:
            by_pay[k] += b.get(k, 0) or 0
        d = by_day.setdefault(b.get("date"), {"date": b.get("date"), "total": 0, "nlc": 0, "bills": 0, "resale": 0})
        d["total"] += t
        d["nlc"] += b.get("nlc", 0) or 0
        d["bills"] += 1
        s = by_staff.setdefault(b.get("staff_name", "—"), {"staff": b.get("staff_name", "—"), "total": 0, "nlc": 0, "bills": 0, "resale": 0})
        s["total"] += t
        s["nlc"] += b.get("nlc", 0) or 0
        s["bills"] += 1
    exq = {"company_id": company, "status": "resold", "resold_date": {"$gte": frm, "$lte": to}}
    if not seesall:
        exq["seller_staff_id"] = user["_id"]
    async for x in db.dc_exchanges.find(exq):
        amt = x.get("resale_amount", 0) or 0
        tot["resale"] += amt
        d = by_day.setdefault(x.get("resold_date"), {"date": x.get("resold_date"), "total": 0, "nlc": 0, "bills": 0, "resale": 0})
        d["resale"] += amt
        s = by_staff.setdefault(x.get("seller_staff_name", "—"), {"staff": x.get("seller_staff_name", "—"), "total": 0, "nlc": 0, "bills": 0, "resale": 0})
        s["resale"] += amt
    eq = {"company_id": company, "date": {"$gte": frm, "$lte": to}, "status": {"$ne": "pending"}}
    if not seesall:
        eq["staff_id"] = user["_id"]
    exp_total, by_cat = 0.0, {}
    async for e in db.dc_expenses.find(eq):
        a = e.get("amount", 0) or 0
        exp_total += a
        by_cat[e.get("category", "—")] = round(by_cat.get(e.get("category", "—"), 0) + a)
    days = sorted(by_day.values(), key=lambda x: x["date"], reverse=True)
    staff = sorted(by_staff.values(), key=lambda x: -x["total"])

    def clean(rows):
        out = []
        for r in rows:
            r = dict(r)
            r["total"] = round(r["total"])
            if admin:
                r["profit"] = round(r["total"] - r.get("nlc", 0) + r.get("resale", 0))
            r.pop("nlc", None)
            r.pop("resale", None)
            out.append(r)
        return out
    res = {"from": frm, "to": to, "totals": {"total": round(tot["total"]), "pending": round(tot["pending"]),
           "bills": tot["bills"], "expenses": round(exp_total)},
           "by_pay": {k: round(v) for k, v in by_pay.items()},
           "by_day": clean(days), "by_staff": clean(staff), "by_category": by_cat}
    if admin:
        res["totals"]["nlc"] = round(tot["nlc"])
        res["totals"]["resale"] = round(tot["resale"])
        res["totals"]["profit"] = round(tot["total"] - tot["nlc"] + tot["resale"])
    return res


# ---------------- Generic CRUD for the list-based modules ----------------
DC_COLLS = {"reminders", "defective", "schemes", "pricelist", "todo", "crm", "attendance", "ledger", "audits"}


def _coll(name):
    if name not in DC_COLLS:
        raise HTTPException(404, "Unknown module")
    return db["dc_" + name]


@router.get("/coll/{name}")
async def coll_list(name: str, company=Depends(dc_company), _=Depends(get_current_user)):
    rows = []
    async for d in _coll(name).find({"company_id": company}).sort("created_at", -1):
        d["id"] = d.pop("_id")
        d.pop("company_id", None)
        rows.append(d)
    return {"rows": rows}


@router.post("/coll/{name}")
async def coll_create(name: str, body: dict = Body(...), company=Depends(dc_company), user=Depends(get_current_user)):
    doc = {k: v for k, v in (body or {}).items() if k not in ("_id", "id", "company_id")}
    doc.update({"_id": uuid.uuid4().hex, "company_id": company,
                "staff_id": user["_id"], "staff_name": user.get("name", ""),
                "created_at": datetime.now(timezone.utc).isoformat()})
    await _coll(name).insert_one(doc)
    doc["id"] = doc.pop("_id")
    doc.pop("company_id", None)
    return doc


@router.patch("/coll/{name}/{rid}")
async def coll_update(name: str, rid: str, body: dict = Body(...), company=Depends(dc_company), _=Depends(get_current_user)):
    upd = {k: v for k, v in (body or {}).items() if k not in ("_id", "id", "company_id")}
    if not upd:
        raise HTTPException(400, "Nothing to update")
    r = await _coll(name).find_one_and_update({"_id": rid, "company_id": company}, {"$set": upd})
    if not r:
        raise HTTPException(404, "Not found")
    return {"ok": True}


@router.delete("/coll/{name}/{rid}")
async def coll_delete(name: str, rid: str, company=Depends(dc_company), _=Depends(get_current_user)):
    await _coll(name).delete_one({"_id": rid, "company_id": company})
    return {"ok": True}
