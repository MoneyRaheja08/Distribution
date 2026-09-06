import uuid

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument

from ..auth import get_current_user, require_roles
from ..db import db
from ..models import CompanyIn, CompanyPatch
from ..serializers import public_company

router = APIRouter(prefix="/companies", tags=["companies"])

# Collections that carry a company_id and get backfilled when the first company is made.
SCOPED = ["dealers", "bills", "payments", "visits", "stock", "pricelists", "products"]


@router.get("")
async def list_companies(user=Depends(get_current_user)):
    q = {} if user["role"] == "admin" else {"_id": {"$in": user.get("company_ids") or []}}
    return [public_company(c) async for c in db.companies.find(q).sort("name", 1)]


@router.post("")
async def create_company(body: CompanyIn, _=Depends(require_roles("admin"))):
    existed = await db.companies.count_documents({})
    cid = uuid.uuid4().hex
    await db.companies.insert_one({"_id": cid, "name": body.name.strip()})
    # First company ever: adopt all existing (untagged) data so nothing disappears.
    if existed == 0:
        for coll in SCOPED:
            await db[coll].update_many({"company_id": {"$exists": False}}, {"$set": {"company_id": cid}})
    return public_company({"_id": cid, "name": body.name.strip()})


@router.patch("/{cid}")
async def rename_company(cid: str, body: CompanyPatch, _=Depends(require_roles("admin"))):
    if body.name is None:
        raise HTTPException(400, "Nothing to update")
    c = await db.companies.find_one_and_update({"_id": cid}, {"$set": {"name": body.name.strip()}},
                                               return_document=ReturnDocument.AFTER)
    if not c:
        raise HTTPException(404, "Company not found")
    return public_company(c)


@router.delete("/{cid}")
async def delete_company(cid: str, _=Depends(require_roles("admin"))):
    if await db.dealers.count_documents({"company_id": cid}) > 0:
        raise HTTPException(400, "Remove this company's dealers before deleting it")
    await db.companies.delete_one({"_id": cid})
    return {"ok": True}
