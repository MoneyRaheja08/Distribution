import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from ..auth import create_token, get_current_user, hash_pin, verify_pin
from ..db import db
from ..models import BootstrapIn, LoginIn
from ..serializers import public_user

router = APIRouter(prefix="/auth", tags=["auth"])

# Slow down PIN guessing: after 8 wrong tries for a name, that name waits 10 minutes.
_FAILS = {}
_MAX_FAILS, _WINDOW = 8, 600


def _throttled(key):
    rec = _FAILS.get(key)
    if not rec:
        return False
    if time.time() - rec[1] > _WINDOW:
        _FAILS.pop(key, None)
        return False
    return rec[0] >= _MAX_FAILS


def _fail(key):
    n, t0 = _FAILS.get(key, (0, time.time()))
    if time.time() - t0 > _WINDOW:
        n, t0 = 0, time.time()
    if len(_FAILS) > 5000:          # never let made-up names grow this without limit
        _FAILS.clear()
    _FAILS[key] = (n + 1, t0)


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
    key = body.name.strip().lower()
    if _throttled(key):
        raise HTTPException(429, "Too many wrong attempts — try again in 10 minutes")
    user = await db.users.find_one({"name": body.name.strip()})
    if not user or not verify_pin(body.pin, user.get("pin_hash") or ""):
        _fail(key)
        raise HTTPException(401, "Wrong name or PIN")
    _FAILS.pop(key, None)
    return {
        "access_token": create_token(user["_id"], user["role"]),
        "token_type": "bearer",
        "user": public_user(user),
    }


@router.get("/me")
async def me(user=Depends(get_current_user)):
    return {"user": public_user(user)}
