from datetime import date

from fastapi import APIRouter, Depends

from ..auth import require_roles
from ..db import db

router = APIRouter(prefix="/visits", tags=["visits"])
staff_only = require_roles("admin", "manager")


@router.get("/today")
async def visits_today(_=Depends(staff_only)):
    today = date.today().isoformat()
    out = []
    async for v in db.visits.find({"date": today}).sort("last_ts", -1):
        out.append({"dealer_name": v.get("dealer_name"), "user_name": v.get("user_name"),
                    "role": v.get("role"), "first_ts": v.get("first_ts"), "last_ts": v.get("last_ts")})
    return out
