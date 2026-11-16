"""Shared data loading for reports. Bills and payments are fetched at the same time
instead of one after the other, which roughly halves the wait on the database."""
import asyncio

from .db import db


async def ledger_maps(company):
    """({dealer_id: [bills]}, {dealer_id: [payments]}) for a whole company."""
    bills, pays = await asyncio.gather(
        db.bills.find({"company_id": company}).to_list(None),
        db.payments.find({"company_id": company}).to_list(None),
    )
    bills_by, pays_by = {}, {}
    for b in bills:
        bills_by.setdefault(b["dealer_id"], []).append(b)
    for p in pays:
        pays_by.setdefault(p["dealer_id"], []).append(p)
    return bills_by, pays_by
