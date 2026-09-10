from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .db import db
from .routers import auth, backup, bills, catalog, companies, dealers, invoices, orders, payments, pricelists, reports, stock, users, visits


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best-effort indexes; ignore if Mongo is briefly unreachable at boot.
    try:
        await db.users.create_index("name", unique=True)
        await db.dealers.create_index("collector_id")
        await db.dealers.create_index("company_id")
        await db.orders.create_index([("company_id", 1), ("status", 1)])
        await db.pricelists.create_index("name")
        await db.pricelists.create_index("company_id")
        # Ledger hot paths: nearly every read filters by company_id and dealer_id/date.
        await db.bills.create_index([("company_id", 1), ("dealer_id", 1)])
        await db.bills.create_index([("company_id", 1), ("date", 1)])
        await db.payments.create_index([("company_id", 1), ("dealer_id", 1)])
        await db.payments.create_index([("company_id", 1), ("date", 1)])
        await db.payments.create_index([("collector_id", 1), ("date", 1)])
        await db.visits.create_index([("company_id", 1), ("date", 1)])
        await db.products.create_index([("pricelist_id", 1)])
        await db.products.create_index([("company_id", 1)])
        # Catalog / profit hot paths.
        await db.sales.create_index([("company_id", 1), ("date", 1)])
        await db.purchases.create_index([("company_id", 1)])
        await db.stock_units.create_index([("company_id", 1), ("status", 1)])
        await db.stock_units.create_index([("company_id", 1), ("imei", 1)])
        await db.stock_lots.create_index([("company_id", 1)])
        await db.import_batches.create_index([("company_id", 1)])
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
app.include_router(companies.router)
app.include_router(users.router)
app.include_router(dealers.router)
app.include_router(stock.router)
app.include_router(payments.router)
app.include_router(pricelists.router)
app.include_router(bills.router)
app.include_router(invoices.router)
app.include_router(orders.router)
app.include_router(visits.router)
app.include_router(reports.router)
app.include_router(catalog.router)
app.include_router(backup.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Ensure crashes still carry CORS headers so browsers surface the real
    # error instead of an opaque "Failed to fetch" on cross-origin deploys.
    origin = request.headers.get("origin", "*")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Server error: {type(exc).__name__}: {exc}"},
        headers={"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true"},
    )


@app.get("/health")
async def health():
    return {"ok": True}
