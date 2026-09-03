import uuid

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument

from ..auth import get_current_user, require_roles
from ..db import db
from ..models import PriceListIn, PriceListPatch, ProductBulk
from ..serializers import public_pricelist, public_product

router = APIRouter(prefix="/pricelists", tags=["pricelists"])
staff_only = require_roles("admin", "manager")


def can_access(user, pl) -> bool:
    return user["role"] == "admin" or user["_id"] in (pl.get("allowed_user_ids") or [])


@router.get("")
async def list_pricelists(user=Depends(get_current_user)):
    out = []
    async for pl in db.pricelists.find().sort("name", 1):
        if can_access(user, pl):
            count = await db.products.count_documents({"pricelist_id": pl["_id"]})
            out.append(public_pricelist(pl, count))
    return out


@router.post("")
async def create_pricelist(body: PriceListIn, _=Depends(staff_only)):
    pl = {"_id": uuid.uuid4().hex, "name": body.name.strip(), "allowed_user_ids": body.allowed_user_ids or []}
    await db.pricelists.insert_one(pl)
    return public_pricelist(pl, 0)


@router.patch("/{plid}")
async def update_pricelist(plid: str, body: PriceListPatch, _=Depends(staff_only)):
    upd = {}
    if body.name is not None:
        upd["name"] = body.name.strip()
    if body.allowed_user_ids is not None:
        upd["allowed_user_ids"] = body.allowed_user_ids
    if not upd:
        raise HTTPException(400, "Nothing to update")
    pl = await db.pricelists.find_one_and_update({"_id": plid}, {"$set": upd}, return_document=ReturnDocument.AFTER)
    if not pl:
        raise HTTPException(404, "Price list not found")
    count = await db.products.count_documents({"pricelist_id": plid})
    return public_pricelist(pl, count)


@router.delete("/{plid}")
async def delete_pricelist(plid: str, _=Depends(require_roles("admin"))):
    await db.products.delete_many({"pricelist_id": plid})
    await db.pricelists.delete_one({"_id": plid})
    return {"ok": True}


@router.get("/{plid}/products")
async def list_products(plid: str, user=Depends(get_current_user)):
    pl = await db.pricelists.find_one({"_id": plid})
    if not pl:
        raise HTTPException(404, "Price list not found")
    if not can_access(user, pl):
        raise HTTPException(403, "You do not have access to this price list")
    return [public_product(p, include_nlc=True) async for p in db.products.find({"pricelist_id": plid}).sort("model", 1)]


@router.post("/{plid}/products/bulk")
async def bulk_replace(plid: str, body: ProductBulk, _=Depends(staff_only)):
    pl = await db.pricelists.find_one({"_id": plid})
    if not pl:
        raise HTTPException(404, "Price list not found")
    await db.products.delete_many({"pricelist_id": plid})
    if body.products:
        docs = [
            {"_id": uuid.uuid4().hex, "pricelist_id": plid, "category": p.category, "model": p.model,
             "description": p.description or "", "mrp": p.mrp, "dp": p.dp, "nlc": p.nlc}
            for p in body.products
        ]
        await db.products.insert_many(docs)
    return {"ok": True, "count": len(body.products)}
