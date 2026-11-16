import traceback
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from .config import check_production, settings
from .db import db
from .routers import auth, backup, bills, catalog, cn, companies, costs, daily_collections, dealers, invoices, orders, payables, payments, pricelists, reports, reports2, stock, targets, users, visits


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_production()
    # Best-effort indexes; ignore if Mongo is briefly unreachable at boot.
    try:
        await db.users.create_index("name", unique=True)
        await db.dealers.create_index("collector_id")
        await db.dealers.create_index("company_id")
        await db.orders.create_index([("company_id", 1), ("status", 1)])
        await db.payments.create_index([("collector_id", 1), ("date", 1)])
        await db.pricelists.create_index("name")
        await db.bills.create_index("dealer_id")
        await db.payments.create_index("dealer_id")
        # --- extra speed indexes (hot query paths) ---
        await db.pricelists.create_index("company_id")
        await db.bills.create_index([("company_id", 1), ("dealer_id", 1)])
        await db.bills.create_index([("company_id", 1), ("date", 1)])
        await db.payments.create_index([("company_id", 1), ("dealer_id", 1)])
        await db.payments.create_index([("company_id", 1), ("date", 1)])
        await db.visits.create_index([("company_id", 1), ("date", 1)])
        await db.products.create_index([("pricelist_id", 1)])
        await db.products.create_index([("company_id", 1)])
        await db.sales.create_index([("company_id", 1), ("date", 1)])
        await db.purchases.create_index([("company_id", 1)])
        await db.stock_units.create_index([("company_id", 1), ("status", 1)])
        await db.stock_units.create_index([("company_id", 1), ("imei", 1)])
        await db.stock_lots.create_index([("company_id", 1)])
        await db.import_batches.create_index([("company_id", 1)])
        await db.sales.create_index([("company_id", 1), ("dealer_id", 1), ("bill_no", 1)])
        await db.purchases.create_index([("company_id", 1), ("date", 1)])
        await db.stock_units.create_index([("company_id", 1), ("status", 1), ("sale_date", 1)])
        await db.payments.create_index([("company_id", 1), ("approved", 1)])
        await db.audit_log.create_index([("company_id", 1), ("ts", -1)])
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

# Reports can be several hundred KB of JSON; compressing them makes phones on mobile data much faster.
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.include_router(auth.router)
app.include_router(companies.router)
app.include_router(users.router)
app.include_router(dealers.router)
app.include_router(stock.router)
app.include_router(payments.router)
app.include_router(pricelists.router)
app.include_router(bills.router)
app.include_router(cn.router)
app.include_router(invoices.router)
app.include_router(orders.router)
app.include_router(visits.router)
app.include_router(reports.router)
app.include_router(reports2.router)
app.include_router(catalog.router)
app.include_router(payables.router)
app.include_router(daily_collections.router)
app.include_router(backup.router)
app.include_router(targets.router)
app.include_router(costs.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # The full error goes to the server log (Render -> Logs) under a short reference; the browser
    # gets only that reference. CORS headers are kept so the message still reaches the page.
    ref = uuid.uuid4().hex[:8]
    print(f"ERROR {ref} {request.method} {request.url.path}\n" + "".join(traceback.format_exception(exc)), flush=True)
    headers = {}
    origin = request.headers.get("origin")
    if origin and (origins == ["*"] or origin in origins):
        headers = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true"}
    return JSONResponse(status_code=500, headers=headers,
                        content={"detail": f"Something went wrong on the server — please try again (ref {ref})"})


@app.get("/health")
async def health():
    return {"ok": True}
