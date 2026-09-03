import uuid

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument

from ..auth import get_current_user, require_roles
from ..db import db
from ..models import StockIn, StockPatch
from ..serializers import public_stock

router = APIRouter(prefix="/stock", tags=["stock"])
staff_only = require_roles("admin", "manager")


@router.get("")
async def list_stock(_=Depends(get_current_user)):
    return [public_stock(s) async for s in db.stock.find()]


@router.post("")
async def create_stock(body: StockIn, _=Depends(staff_only)):
    s = {"_id": uuid.uuid4().hex, "name": body.name.strip(), "price": body.price, "qty": body.qty}
    await db.stock.insert_one(s)
    return public_stock(s)


@router.patch("/{sid}")
async def update_stock(sid: str, body: StockPatch, _=Depends(staff_only)):
    upd = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if "name" in upd:
        upd["name"] = upd["name"].strip()
    if not upd:
        raise HTTPException(400, "Nothing to update")
    s = await db.stock.find_one_and_update(
        {"_id": sid}, {"$set": upd}, return_document=ReturnDocument.AFTER
    )
    if not s:
        raise HTTPException(404, "Product not found")
    return public_stock(s)


@router.delete("/{sid}")
async def delete_stock(sid: str, _=Depends(staff_only)):
    await db.stock.delete_one({"_id": sid})
    return {"ok": True}
