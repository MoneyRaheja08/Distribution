import uuid
from datetime import datetime, timezone, date
from typing import Optional, List

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from ..auth import get_current_user
from ..deps import current_company
from ..db import db

router = APIRouter(prefix="/dc", tags=["daily-collections"])


async def dc_company(company=Depends(current_company)):
    c = await db.companies.find_one({"_id": company})
    if not c or c.get("kind") != "daily_collections":
        raise HTTPException(400, "Not a Daily Collections company")
    return company


def _today():
    return date.today().isoformat()


def _is_admin(u):
    return u.get("role") == "admin"


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
    total: float = 0
    cash: float = 0
    card: float = 0
    upi: float = 0
    finance: float = 0
    cheque: float = 0
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
    cheque: Optional[float] = None
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


EDIT_WINDOW_SEC = 120


def _editable_until(b):
    try:
        return datetime.fromisoformat(b["created_at"]).timestamp() + EDIT_WINDOW_SEC
    except Exception:
        return 0


def _pub_bill(b, admin):
    until = _editable_until(b)
    out = {
        "editable_until": until, "locked": datetime.now(timezone.utc).timestamp() > until,
        "id": b["_id"], "date": b.get("date"), "bill_no": b.get("bill_no", ""),
        "customer": b.get("customer", ""), "phone": b.get("phone", ""),
        "items": b.get("items", []), "total": b.get("total", 0),
        "cash": b.get("cash", 0), "card": b.get("card", 0), "upi": b.get("upi", 0),
        "finance": b.get("finance", 0), "cheque": b.get("cheque", 0),
        "pending": b.get("pending", 0), "note": b.get("note", ""),
        "staff": b.get("staff_name", ""), "created_at": b.get("created_at"),
    }
    if admin:
        out["nlc"] = b.get("nlc", 0)
        out["profit"] = round((b.get("total", 0) or 0) - (b.get("nlc", 0) or 0))
    return out


# ---------------- Bills ----------------
@router.get("/bills")
async def list_bills(date: Optional[str] = None, frm: Optional[str] = None, to: Optional[str] = None,
                     company=Depends(dc_company), user=Depends(get_current_user)):
    q = {"company_id": company}
    if date:
        q["date"] = date
    elif frm and to:
        q["date"] = {"$gte": frm, "$lte": to}
    admin = _is_admin(user)
    rows = [_pub_bill(b, admin) async for b in db.dc_bills.find(q).sort("created_at", -1)]
    tot = {"total": sum(r["total"] for r in rows), "cash": sum(r["cash"] for r in rows),
           "card": sum(r["card"] for r in rows), "upi": sum(r["upi"] for r in rows),
           "finance": sum(r["finance"] for r in rows), "cheque": sum(r["cheque"] for r in rows),
           "pending": sum(r["pending"] for r in rows), "bills": len(rows)}
    if admin:
        tot["nlc"] = sum(r.get("nlc", 0) for r in rows)
        tot["profit"] = round(tot["total"] - tot["nlc"])
    return {"rows": rows, "totals": tot, "is_admin": admin}


@router.post("/bills")
async def create_bill(body: BillIn, company=Depends(dc_company), user=Depends(get_current_user)):
    doc = body.model_dump()
    doc["items"] = [i for i in doc.get("items", [])]
    doc.update({"_id": uuid.uuid4().hex, "company_id": company, "date": body.date or _today(),
                "staff_id": user["_id"], "staff_name": user.get("name", ""),
                "created_at": datetime.now(timezone.utc).isoformat()})
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
    field = mode if mode in ("cash", "card", "upi", "finance", "cheque") else "cash"
    await db.dc_bills.update_one({"_id": bid}, {"$set": {"pending": new_pending},
                                                "$inc": {field: amt}})
    b["pending"] = new_pending
    b[field] = (b.get(field, 0) or 0) + amt
    return _pub_bill(b, _is_admin(user))


@router.get("/calendar")
async def dc_calendar(month: str, company=Depends(dc_company), user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    q = {"company_id": company, "date": {"$gte": month + "-01", "$lte": month + "-31"}}
    days = {}
    async for b in db.dc_bills.find(q):
        d = days.setdefault(b["date"], {"date": b["date"], "total": 0, "bills": 0, "nlc": 0, "pending": 0})
        d["total"] += b.get("total", 0) or 0
        d["bills"] += 1
        d["nlc"] += b.get("nlc", 0) or 0
        d["pending"] += b.get("pending", 0) or 0
    async for e in db.dc_expenses.find(q):
        d = days.setdefault(e["date"], {"date": e["date"], "total": 0, "bills": 0, "nlc": 0, "pending": 0})
        d["expenses"] = d.get("expenses", 0) + (e.get("amount", 0) or 0)
    rows = []
    for d in sorted(days.values(), key=lambda x: x["date"]):
        d["profit"] = round(d["total"] - d.pop("nlc"))
        d["total"] = round(d["total"]); d["pending"] = round(d["pending"]); d["expenses"] = round(d.get("expenses", 0))
        rows.append(d)
    tot = round(sum(r["total"] for r in rows))
    return {"month": month, "days": rows, "total": tot, "bills": sum(r["bills"] for r in rows),
            "profit": round(sum(r["profit"] for r in rows)), "best": max(rows, key=lambda r: r["total"])["date"] if rows else None}


@router.delete("/bills/{bid}")
async def delete_bill(bid: str, company=Depends(dc_company), _=Depends(get_current_user)):
    await db.dc_bills.delete_one({"_id": bid, "company_id": company})
    return {"ok": True}


@router.get("/pending")
async def pending(frm: Optional[str] = None, to: Optional[str] = None, mine: bool = False,
                  company=Depends(dc_company), user=Depends(get_current_user)):
    q = {"company_id": company, "pending": {"$gt": 0}}
    if frm and to:
        q["date"] = {"$gte": frm, "$lte": to}
    if mine:
        q["staff_id"] = user["_id"]
    admin = _is_admin(user)
    rows = [_pub_bill(b, admin) async for b in db.dc_bills.find(q).sort("date", 1)]
    return {"rows": rows, "total": sum(r["pending"] for r in rows), "count": len(rows)}


# ---------------- Expenses ----------------
@router.get("/expenses")
async def list_expenses(date: Optional[str] = None, month: Optional[str] = None,
                        company=Depends(dc_company), _=Depends(get_current_user)):
    q = {"company_id": company}
    if date:
        q["date"] = date
    elif month:
        q["date"] = {"$regex": "^" + month}
    rows = []
    async for e in db.dc_expenses.find(q).sort("created_at", -1):
        rows.append({"id": e["_id"], "date": e.get("date"), "category": e.get("category", ""),
                     "amount": e.get("amount", 0), "paid_by": e.get("paid_by", ""),
                     "note": e.get("note", ""), "staff": e.get("staff_name", "")})
    return {"rows": rows, "total": round(sum(r["amount"] for r in rows))}


@router.post("/expenses")
async def create_expense(body: ExpenseIn, company=Depends(dc_company), user=Depends(get_current_user)):
    eid = uuid.uuid4().hex
    doc = body.model_dump()
    doc.update({"_id": eid, "company_id": company, "date": body.date or _today(),
                "staff_id": user["_id"], "staff_name": user.get("name", ""),
                "created_at": datetime.now(timezone.utc).isoformat()})
    await db.dc_expenses.insert_one(doc)
    return {"id": eid, "date": doc["date"], "category": doc["category"],
            "amount": doc["amount"], "paid_by": doc["paid_by"], "note": doc["note"], "staff": doc["staff_name"]}


@router.delete("/expenses/{eid}")
async def delete_expense(eid: str, company=Depends(dc_company), _=Depends(get_current_user)):
    await db.dc_expenses.delete_one({"_id": eid, "company_id": company})
    return {"ok": True}


# ---------------- Day close / reconciliation ----------------
@router.get("/day")
async def day_summary(date: Optional[str] = None, company=Depends(dc_company), user=Depends(get_current_user)):
    d = date or _today()
    admin = _is_admin(user)
    bills = [b async for b in db.dc_bills.find({"company_id": company, "date": d})]
    exp = [e async for e in db.dc_expenses.find({"company_id": company, "date": d})]
    cash_in = sum(b.get("cash", 0) or 0 for b in bills)
    total = sum(b.get("total", 0) or 0 for b in bills)
    pend = sum(b.get("pending", 0) or 0 for b in bills)
    exp_total = sum(e.get("amount", 0) or 0 for e in exp)
    r = await db.dc_recon.find_one({"company_id": company, "date": d}) or {}
    float_open = r.get("float_open", 0) or 0
    handover = r.get("handover", 0) or 0
    expected = float_open + cash_in - exp_total
    out = {
        "date": d, "bills": len(bills), "total": round(total), "cash_in": round(cash_in),
        "card": round(sum(b.get("card", 0) or 0 for b in bills)),
        "upi": round(sum(b.get("upi", 0) or 0 for b in bills)),
        "finance": round(sum(b.get("finance", 0) or 0 for b in bills)),
        "cheque": round(sum(b.get("cheque", 0) or 0 for b in bills)),
        "pending": round(pend), "expenses": round(exp_total),
        "float_open": round(float_open), "handover": round(handover),
        "expected_in_drawer": round(expected), "diff": round(expected - handover),
        "note": r.get("note", ""),
    }
    if admin:
        nlc = sum(b.get("nlc", 0) or 0 for b in bills)
        out["nlc"] = round(nlc)
        out["profit"] = round(total - nlc)
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
    by_day, by_staff, by_pay = {}, {}, {"cash": 0, "card": 0, "upi": 0, "finance": 0, "cheque": 0}
    tot = {"total": 0.0, "pending": 0.0, "nlc": 0.0, "bills": 0}
    async for b in db.dc_bills.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
        t = b.get("total", 0) or 0
        tot["total"] += t
        tot["pending"] += b.get("pending", 0) or 0
        tot["nlc"] += b.get("nlc", 0) or 0
        tot["bills"] += 1
        for k in by_pay:
            by_pay[k] += b.get(k, 0) or 0
        d = by_day.setdefault(b.get("date"), {"date": b.get("date"), "total": 0, "nlc": 0, "bills": 0})
        d["total"] += t
        d["nlc"] += b.get("nlc", 0) or 0
        d["bills"] += 1
        s = by_staff.setdefault(b.get("staff_name", "—"), {"staff": b.get("staff_name", "—"), "total": 0, "nlc": 0, "bills": 0})
        s["total"] += t
        s["nlc"] += b.get("nlc", 0) or 0
        s["bills"] += 1
    exp_total, by_cat = 0.0, {}
    async for e in db.dc_expenses.find({"company_id": company, "date": {"$gte": frm, "$lte": to}}):
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
                r["profit"] = round(r["total"] - r.get("nlc", 0))
            r.pop("nlc", None)
            out.append(r)
        return out
    res = {"from": frm, "to": to, "totals": {"total": round(tot["total"]), "pending": round(tot["pending"]),
           "bills": tot["bills"], "expenses": round(exp_total)},
           "by_pay": {k: round(v) for k, v in by_pay.items()},
           "by_day": clean(days), "by_staff": clean(staff), "by_category": by_cat}
    if admin:
        res["totals"]["nlc"] = round(tot["nlc"])
        res["totals"]["profit"] = round(tot["total"] - tot["nlc"])
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
