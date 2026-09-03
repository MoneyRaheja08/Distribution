import uuid

from fastapi import APIRouter, HTTPException

from ..auth import create_token, hash_pin, verify_pin
from ..db import db
from ..models import BootstrapIn, LoginIn
from ..serializers import public_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/bootstrap")
async def bootstrap(body: BootstrapIn):
    """One-time: create the first admin. Fails once any user exists."""
    if await db.users.count_documents({}) > 0:
        raise HTTPException(400, "Already initialised")
    uid = uuid.uuid4().hex
    user = {"_id": uid, "name": body.name.strip(), "pin_hash": hash_pin(body.pin), "role": "admin"}
    await db.users.insert_one(user)
    return {"access_token": create_token(uid, "admin"), "token_type": "bearer", "user": public_user(user)}


@router.post("/login")
async def login(body: LoginIn):
    user = await db.users.find_one({"name": body.name.strip()})
    if not user or not verify_pin(body.pin, user["pin_hash"]):
        raise HTTPException(401, "Wrong name or PIN")
    return {
        "access_token": create_token(user["_id"], user["role"]),
        "token_type": "bearer",
        "user": public_user(user),
    }
