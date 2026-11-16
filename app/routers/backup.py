"""Backups.
GET  /backup          the old Excel-friendly export (dealers, bills, payments of one company)
GET  /backup/info     record counts
GET  /backup/full     EVERYTHING in the database, all companies, as one JSON file
POST /backup/restore  load a /backup/full file back (admin; must type RESTORE)
GET  /backup/audit    the audit trail for the selected company
"""
import re
from datetime import datetime, timedelta, timezone

from bson import json_util
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile

from ..audit import record as audit
from ..auth import require_roles
from ..db import db
from ..deps import current_company

router = APIRouter(prefix="/backup", tags=["backup"])
admin_only = require_roles("admin")

APP_TAG = "ashoka-distribution"
VERSION = 1
NAME_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
NEVER_RESTORED = {"audit_log"}      # the trail of what happened is never overwritten by a restore


@router.get("")
async def backup(company=Depends(current_company), _=Depends(admin_only)):
    dealers = [d async for d in db.dealers.find({"company_id": company})]
    dmap = {d["_id"]: d["name"] for d in dealers}
    users = {u["_id"]: u["name"] async for u in db.users.find()}
    return {
        "dealers": [{"name": d["name"], "area": d.get("area"), "phone": d.get("phone"),
                     "credit_limit": d.get("credit_limit", 0),
                     "collector": users.get(d.get("collector_id"), "")} for d in dealers],
        "bills": [{"dealer": dmap.get(b["dealer_id"], "?"), "bill_no": b.get("bill_no"),
                   "date": b.get("date"), "amount": b.get("amount")} async for b in db.bills.find({"company_id": company})],
        "payments": [{"dealer": p.get("dealer_name"), "amount": p.get("amount"), "mode": p.get("mode"),
                      "cheque": p.get("cheque"), "date": p.get("date"), "status": p.get("status"),
                      "collector": p.get("collector_name"), "approved": p.get("approved", True),
                      "reconciled": p.get("reconciled", False)} async for p in db.payments.find({"company_id": company})],
    }


async def _names():
    return sorted(n for n in await db.list_collection_names() if NAME_OK.match(n) and not n.startswith("system"))


@router.get("/info")
async def backup_info(_=Depends(admin_only)):
    counts = {n: await db[n].count_documents({}) for n in await _names()}
    return {"counts": counts, "total": sum(counts.values())}


@router.get("/full")
async def backup_full(user=Depends(admin_only)):
    collections = {n: await db[n].find({}).to_list(None) for n in await _names()}
    payload = {"app": APP_TAG, "version": VERSION, "exported_at": datetime.now(timezone.utc),
               "exported_by": user.get("name"), "counts": {n: len(v) for n, v in collections.items()},
               "collections": collections}
    # json_util keeps dates as real dates, so a restored database is identical to the original
    return Response(json_util.dumps(payload), media_type="application/json")


@router.post("/restore")
async def backup_restore(file: UploadFile = File(...), mode: str = Form("merge"), confirm: str = Form(""),
                         user=Depends(admin_only)):
    if confirm != "RESTORE":
        raise HTTPException(400, "Type RESTORE to confirm")
    if mode not in ("merge", "replace"):
        raise HTTPException(400, "Bad mode")
    try:
        data = json_util.loads((await file.read()).decode("utf-8-sig"))
    except Exception:
        raise HTTPException(400, "That file isn't a valid backup")
    if not (isinstance(data, dict) and data.get("app") == APP_TAG and isinstance(data.get("collections"), dict)):
        raise HTTPException(400, "This is not a full backup made by this app (use the JSON file from 'Download full backup')")

    plan, skipped = {}, []
    for name, docs in data["collections"].items():
        if not NAME_OK.match(str(name)) or str(name).startswith("system") or name in NEVER_RESTORED or not isinstance(docs, list):
            skipped.append(str(name))
            continue
        plan[name] = [d for d in docs if isinstance(d, dict) and d.get("_id") is not None]
    if not sum(len(v) for v in plan.values()):
        raise HTTPException(400, "No records found in the file")

    me_before = await db.users.find_one({"_id": user["_id"]})
    before = {n: await db[n].count_documents({}) for n in plan}
    for name, docs in plan.items():
        if mode == "replace":
            await db[name].delete_many({})
        if name == "users":     # names are unique: an account in the file replaces one with the same name
            await db.users.delete_many({"name": {"$in": [d.get("name") for d in docs]}})
        for i in range(0, len(docs), 500):
            chunk = docs[i:i + 500]
            await db[name].delete_many({"_id": {"$in": [d["_id"] for d in chunk]}})
            await db[name].insert_many(chunk)

    # whoever ran the restore keeps the login they just used
    if me_before:
        keep = {"role": "admin", "pin_hash": me_before.get("pin_hash"), "name": me_before.get("name")}
        clash = await db.users.find_one({"name": me_before.get("name"), "_id": {"$ne": user["_id"]}})
        if clash:       # same name under another id in the file: the current account wins
            await db.users.delete_one({"_id": clash["_id"]})
        if await db.users.find_one({"_id": user["_id"]}):
            await db.users.update_one({"_id": user["_id"]}, {"$set": keep})
        else:
            await db.users.insert_one({**me_before, **keep})

    counts = {n: len(v) for n, v in plan.items() if v}
    total = sum(counts.values())
    at = data.get("exported_at")
    taken = (at.replace(tzinfo=timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%d %b %Y %I:%M %p") if isinstance(at, datetime) else str(at)
    await audit(user, None, "backup.restore",
                f"Restored backup taken {taken} ({mode}): {total} records; before: "
                + ", ".join(f"{n} {c}" for n, c in before.items() if c))
    return {"ok": True, "mode": mode, "counts": counts, "total": total, "skipped": skipped}


@router.get("/audit")
async def audit_list(limit: int = 200, company=Depends(current_company), _=Depends(admin_only)):
    limit = max(1, min(limit, 1000))
    out = []
    async for a in db.audit_log.find({"company_id": {"$in": [company, None]}}, {"removed": 0}).sort("ts", -1).limit(limit):
        out.append({"id": a["_id"], "ts": a["ts"].replace(tzinfo=timezone.utc).isoformat() if isinstance(a.get("ts"), datetime) else a.get("ts"),
                    "user": a.get("user_name"), "action": a.get("action"), "summary": a.get("summary"),
                    "counts": a.get("counts") or {}})
    return out
