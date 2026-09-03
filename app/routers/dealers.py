import uuid

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument

from ..auth import get_current_user, is_staff, require_roles
from ..db import db
from ..models import DealerIn, DealerPatch
from ..serializers import public_dealer

router = APIRouter(prefix="/dealers", tags=["dealers"])
staff_only = require_roles("admin", "manager")


@router.get("")
async def list_dealers(user=Depends(get_current_user)):
    # Collectors see only their assigned dealers; staff see everything.
    query = {} if is_staff(user) else {"collector_id": user["_id"]}
    return [public_dealer(d) async for d in db.dealers.find(query)]


@router.get("/{did}")
async def get_dealer(did: str, user=Depends(get_current_user)):
    d = await db.dealers.find_one({"_id": did})
    if not d:
        raise HTTPException(404, "Dealer not found")
    if not is_staff(user) and d.get("collector_id") != user["_id"]:
        raise HTTPException(403, "Not your dealer")
    return public_dealer(d)


@router.post("")
async def create_dealer(body: DealerIn, _=Depends(staff_only)):
    d = {
        "_id": uuid.uuid4().hex,
        "name": body.name.strip(),
        "area": body.area,
        "phone": body.phone,
        "credit_limit": body.credit_limit,
        "collector_id": body.collector_id,
        "ageing": body.ageing.model_dump(),
    }
    await db.dealers.insert_one(d)
    return public_dealer(d)


@router.patch("/{did}")
async def update_dealer(did: str, body: DealerPatch, _=Depends(staff_only)):
    upd = {}
    for field in ("name", "area", "phone", "credit_limit", "collector_id"):
        val = getattr(body, field)
        if val is not None:
            upd[field] = val.strip() if field == "name" else val
    if body.ageing is not None:
        upd["ageing"] = body.ageing.model_dump()
    if not upd:
        raise HTTPException(400, "Nothing to update")
    d = await db.dealers.find_one_and_update(
        {"_id": did}, {"$set": upd}, return_document=ReturnDocument.AFTER
    )
    if not d:
        raise HTTPException(404, "Dealer not found")
    return public_dealer(d)


@router.delete("/{did}")
async def delete_dealer(did: str, _=Depends(require_roles("admin"))):
    await db.dealers.delete_one({"_id": did})
    return {"ok": True}
