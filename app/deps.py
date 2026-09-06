from fastapi import Depends, Header, HTTPException

from .auth import get_current_user
from .db import db


async def current_company(x_company_id: str = Header(default=None, alias="X-Company-Id"),
                          user=Depends(get_current_user)):
    """Resolve + authorize the active company from the X-Company-Id header."""
    if not x_company_id:
        raise HTTPException(400, "No company selected")
    comp = await db.companies.find_one({"_id": x_company_id})
    if not comp:
        raise HTTPException(404, "Company not found")
    if user["role"] != "admin" and x_company_id not in (user.get("company_ids") or []):
        raise HTTPException(403, "You do not have access to this company")
    return x_company_id
