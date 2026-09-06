from datetime import date, timedelta

from fastapi import APIRouter, Depends

from ..auth import require_roles
from ..deps import current_company
from ..db import db

router = APIRouter(prefix="/visits", tags=["visits"])
staff_only = require_roles("admin", "manager")


def _ist(dt):
    if not dt:
        return None
    d = dt + timedelta(hours=5, minutes=30)   # India is always UTC+5:30
    return d.strftime("%I:%M %p").lstrip("0")


@router.get("/today")
async def visits_today(company=Depends(current_company), _=Depends(staff_only)):
    today = date.today().isoformat()
    out = []
    async for v in db.visits.find({"date": today, "company_id": company}).sort("first_ts", 1):
        out.append({"dealer_name": v.get("dealer_name"), "user_name": v.get("user_name"),
                    "role": v.get("role"), "time": _ist(v.get("first_ts")),
                    "lat": v.get("lat"), "lng": v.get("lng")})
    return out
