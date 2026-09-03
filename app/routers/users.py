import uuid

from fastapi import APIRouter, Depends, HTTPException
from pymongo import ReturnDocument

from ..auth import hash_pin, require_roles
from ..db import db
from ..models import UserIn, UserPatch
from ..serializers import public_user

router = APIRouter(prefix="/users", tags=["users"])
admin_only = require_roles("admin")


@router.get("")
async def list_users(_=Depends(admin_only)):
    return [public_user(u) async for u in db.users.find()]


@router.get("/selectable")
async def selectable_users(_=Depends(require_roles("admin", "manager"))):
    # Minimal list (no PINs) so staff can pick who may access a price list.
    return [{"id": u["_id"], "name": u["name"], "role": u["role"]}
            async for u in db.users.find({"role": {"$ne": "admin"}})]


@router.get("/list")
async def pickable_users(_=Depends(require_roles("admin", "manager"))):
    # Minimal list for the price-list "who can see" picker (staff only).
    return [{"id": u["_id"], "name": u["name"], "role": u["role"]} async for u in db.users.find()]


@router.post("")
async def create_user(body: UserIn, _=Depends(admin_only)):
    if await db.users.find_one({"name": body.name.strip()}):
        raise HTTPException(400, "A user with that name already exists")
    uid = uuid.uuid4().hex
    u = {"_id": uid, "name": body.name.strip(), "pin_hash": hash_pin(body.pin),
         "role": body.role.value, "price_list_access": body.price_list_access}
    await db.users.insert_one(u)
    return public_user(u)


@router.patch("/{uid}")
async def update_user(uid: str, body: UserPatch, _=Depends(admin_only)):
    upd = {}
    if body.name is not None:
        upd["name"] = body.name.strip()
    if body.role is not None:
        upd["role"] = body.role.value
    if body.pin is not None:
        upd["pin_hash"] = hash_pin(body.pin)
    if body.price_list_access is not None:
        upd["price_list_access"] = body.price_list_access
    if not upd:
        raise HTTPException(400, "Nothing to update")
    u = await db.users.find_one_and_update(
        {"_id": uid}, {"$set": upd}, return_document=ReturnDocument.AFTER
    )
    if not u:
        raise HTTPException(404, "User not found")
    return public_user(u)


@router.delete("/{uid}")
async def delete_user(uid: str, me=Depends(admin_only)):
    if uid == me["_id"]:
        raise HTTPException(400, "You cannot delete your own account")
    await db.users.delete_one({"_id": uid})
    return {"ok": True}
