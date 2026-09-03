from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import db
from .routers import auth, dealers, payments, pricelists, stock, users


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best-effort indexes; ignore if Mongo is briefly unreachable at boot.
    try:
        await db.users.create_index("name", unique=True)
        await db.dealers.create_index("collector_id")
        await db.payments.create_index([("collector_id", 1), ("date", 1)])
        await db.pricelists.create_index("name")
    except Exception:
        pass
    yield


app = FastAPI(title="Ashoka Distribution API", version="1.0.0", lifespan=lifespan)

origins = ["*"] if settings.cors_origins.strip() == "*" else [o.strip() for o in settings.cors_origins.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(dealers.router)
app.include_router(stock.router)
app.include_router(payments.router)
app.include_router(pricelists.router)


@app.get("/health")
async def health():
    return {"ok": True}
