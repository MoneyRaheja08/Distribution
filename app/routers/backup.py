from fastapi import APIRouter, Depends

from ..auth import require_roles
from ..db import db

router = APIRouter(prefix="/backup", tags=["backup"])


@router.get("")
async def backup(_=Depends(require_roles("admin"))):
    dealers = [d async for d in db.dealers.find()]
    dmap = {d["_id"]: d["name"] for d in dealers}
    users = {u["_id"]: u["name"] async for u in db.users.find()}
    return {
        "dealers": [{"name": d["name"], "area": d.get("area"), "phone": d.get("phone"),
                     "credit_limit": d.get("credit_limit", 0),
                     "collector": users.get(d.get("collector_id"), "")} for d in dealers],
        "bills": [{"dealer": dmap.get(b["dealer_id"], "?"), "bill_no": b.get("bill_no"),
                   "date": b.get("date"), "amount": b.get("amount")} async for b in db.bills.find()],
        "payments": [{"dealer": p.get("dealer_name"), "amount": p.get("amount"), "mode": p.get("mode"),
                      "cheque": p.get("cheque"), "date": p.get("date"), "status": p.get("status"),
                      "collector": p.get("collector_name"), "approved": p.get("approved", True),
                      "reconciled": p.get("reconciled", False)} async for p in db.payments.find()],
    }
