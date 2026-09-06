import uuid

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument

from ..auth import get_current_user, require_roles
from ..deps import current_company
from ..db import db
from ..models import PriceListIn, PriceListPatch, ProductBulk, ProductIn, ProductPatch, FlexImport, CellRow
from ..serializers import public_pricelist, public_product

router = APIRouter(prefix="/pricelists", tags=["pricelists"])
staff_only = require_roles("admin", "manager")


def can_access(user, pl) -> bool:
    return user["role"] == "admin" or user["_id"] in (pl.get("allowed_user_ids") or [])


@router.get("")
async def list_pricelists(company=Depends(current_company), user=Depends(get_current_user)):
    out = []
    async for pl in db.pricelists.find({"company_id": company}).sort("name", 1):
        if can_access(user, pl):
            count = await db.products.count_documents({"pricelist_id": pl["_id"]})
            out.append(public_pricelist(pl, count))
    return out


@router.post("")
async def create_pricelist(body: PriceListIn, company=Depends(current_company), _=Depends(staff_only)):
    pl = {"_id": uuid.uuid4().hex, "name": body.name.strip(), "allowed_user_ids": body.allowed_user_ids or [], "company_id": company}
    await db.pricelists.insert_one(pl)
    return public_pricelist(pl, 0)


@router.patch("/{plid}")
async def update_pricelist(plid: str, body: PriceListPatch, company=Depends(current_company), _=Depends(staff_only)):
    upd = {}
    if body.name is not None:
        upd["name"] = body.name.strip()
    if body.allowed_user_ids is not None:
        upd["allowed_user_ids"] = body.allowed_user_ids
    if body.columns is not None:
        upd["columns"] = body.columns
    if body.model_col is not None:
        upd["model_col"] = body.model_col
    if body.price_col is not None:
        upd["price_col"] = body.price_col
    if not upd:
        raise HTTPException(400, "Nothing to update")
    pl = await db.pricelists.find_one_and_update({"_id": plid, "company_id": company}, {"$set": upd}, return_document=ReturnDocument.AFTER)
    if not pl:
        raise HTTPException(404, "Price list not found")
    count = await db.products.count_documents({"pricelist_id": plid})
    return public_pricelist(pl, count)


@router.delete("/{plid}")
async def delete_pricelist(plid: str, company=Depends(current_company), _=Depends(require_roles("admin"))):
    await db.products.delete_many({"pricelist_id": plid})
    await db.pricelists.delete_one({"_id": plid})
    return {"ok": True}


@router.get("/{plid}/products")
async def list_products(plid: str, company=Depends(current_company), user=Depends(get_current_user)):
    pl = await db.pricelists.find_one({"_id": plid, "company_id": company})
    if not pl:
        raise HTTPException(404, "Price list not found")
    if not can_access(user, pl):
        raise HTTPException(403, "You do not have access to this price list")
    return [public_product(p) async for p in db.products.find({"pricelist_id": plid})]


@router.post("/{plid}/products/bulk")
async def bulk_replace(plid: str, body: ProductBulk, company=Depends(current_company), _=Depends(staff_only)):
    pl = await db.pricelists.find_one({"_id": plid, "company_id": company})
    if not pl:
        raise HTTPException(404, "Price list not found")
    await db.products.delete_many({"pricelist_id": plid})
    if body.products:
        docs = [
            {"_id": uuid.uuid4().hex, "pricelist_id": plid, "company_id": company, "category": p.category, "model": p.model,
             "description": p.description or "", "mrp": p.mrp, "dp": p.dp, "nlc": p.nlc}
            for p in body.products
        ]
        await db.products.insert_many(docs)
    return {"ok": True, "count": len(body.products)}


@router.post("/{plid}/products/one")
async def add_product(plid: str, body: CellRow, company=Depends(current_company), _=Depends(staff_only)):
    pl = await db.pricelists.find_one({"_id": plid, "company_id": company})
    if not pl:
        raise HTTPException(404, "Price list not found")
    doc = {"_id": uuid.uuid4().hex, "pricelist_id": plid, "company_id": company, "cells": body.cells}
    await db.products.insert_one(doc)
    return public_product(doc)


@router.patch("/{plid}/products/{pid}")
async def update_product(plid: str, pid: str, body: CellRow, company=Depends(current_company), _=Depends(staff_only)):
    if not await db.pricelists.find_one({"_id": plid, "company_id": company}):
        raise HTTPException(404, "Price list not found")
    p = await db.products.find_one_and_update({"_id": pid, "pricelist_id": plid, "company_id": company},
                                              {"$set": {"cells": body.cells}}, return_document=ReturnDocument.AFTER)
    if not p:
        raise HTTPException(404, "Product not found")
    return public_product(p)


@router.delete("/{plid}/products/{pid}")
async def delete_product(plid: str, pid: str, company=Depends(current_company), _=Depends(staff_only)):
    await db.products.delete_one({"_id": pid, "pricelist_id": plid, "company_id": company})
    return {"ok": True}


@router.post("/{plid}/import")
async def flex_import(plid: str, body: FlexImport, company=Depends(current_company), _=Depends(staff_only)):
    """Import a price list keeping ALL columns. Rows are stored as free-form cells."""
    pl = await db.pricelists.find_one({"_id": plid, "company_id": company})
    if not pl:
        raise HTTPException(404, "Price list not found")
    await db.pricelists.update_one({"_id": plid}, {"$set": {
        "columns": body.columns, "model_col": body.model_col, "price_col": body.price_col}})
    await db.products.delete_many({"pricelist_id": plid})
    if body.rows:
        docs = [{"_id": uuid.uuid4().hex, "pricelist_id": plid, "company_id": company, "cells": r} for r in body.rows]
        await db.products.insert_many(docs)
    return {"ok": True, "count": len(body.rows)}


@router.delete("/{plid}/columns/{col}")
async def delete_column(plid: str, col: str, company=Depends(current_company), _=Depends(staff_only)):
    pl = await db.pricelists.find_one({"_id": plid, "company_id": company})
    if not pl:
        raise HTTPException(404, "Price list not found")
    cols = [c for c in (pl.get("columns") or []) if c != col]
    await db.pricelists.update_one({"_id": plid}, {"$set": {"columns": cols}})
    await db.products.update_many({"pricelist_id": plid}, {"$unset": {f"cells.{col}": ""}})
    return {"ok": True}
