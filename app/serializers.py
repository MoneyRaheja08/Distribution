AGE_ALL = ["age_0_30", "age_31_60", "age_61_90", "age_90p"]
# Oldest dues cleared first when a payment comes in.
AGE_OLDEST_FIRST = ["age_90p", "age_61_90", "age_31_60", "age_0_30"]


def outstanding(dealer) -> float:
    ag = dealer.get("ageing") or {}
    return sum(float(ag.get(f, 0) or 0) for f in AGE_ALL)


def allocate(ageing: dict, amount: float) -> dict:
    """Reduce the ageing buckets oldest-first. Mutates `ageing`. Returns what
    was taken from each bucket (used later to restore on a bounced cheque)."""
    alloc = {}
    remaining = amount
    for f in AGE_OLDEST_FIRST:
        avail = float(ageing.get(f, 0) or 0)
        take = min(remaining, avail)
        if take > 0:
            alloc[f] = take
            ageing[f] = avail - take
            remaining -= take
    return alloc


def restore(ageing: dict, alloc: dict) -> None:
    """Add a bounced cheque's allocation back onto the dealer's ageing."""
    for f, val in (alloc or {}).items():
        ageing[f] = float(ageing.get(f, 0) or 0) + float(val)


def public_user(u):
    return {
        "id": u["_id"],
        "name": u["name"],
        "role": u["role"],
        "price_list_access": u.get("price_list_access", False),
        "can_collect": u.get("can_collect", False),
        "can_import_statement": u.get("can_import_statement", False),
        "can_view_reports": u.get("can_view_reports", False),
        "company_ids": u.get("company_ids", []),
    }


def public_dealer(d, summary=None, visited_today=False):
    summary = summary or {"outstanding": 0, "ageing": {}, "last_payment": None}
    return {
        "id": d["_id"],
        "name": d["name"],
        "area": d.get("area"),
        "phone": d.get("phone"),
        "credit_limit": d.get("credit_limit", 0),
        "collector_id": d.get("collector_id"),
        "ageing": summary["ageing"],
        "outstanding": summary["outstanding"],
        "last_payment": summary["last_payment"],
        "visited_today": visited_today,
    }


def public_stock(s):
    return {"id": s["_id"], "name": s["name"], "price": s.get("price", 0), "qty": s.get("qty", 0)}


def public_payment(p):
    return {
        "id": p["_id"],
        "dealer_id": p["dealer_id"],
        "dealer_name": p.get("dealer_name"),
        "collector_id": p["collector_id"],
        "collector_name": p.get("collector_name"),
        "amount": p["amount"],
        "mode": p["mode"],
        "cheque": p.get("cheque"),
        "date": p["date"],
        "receipt": p["receipt"],
        "status": p["status"],
        "deposited": p.get("deposited", False),
        "approved": p.get("approved", True),
        "approved_by": p.get("approved_by"),
        "reconciled": p.get("reconciled", False),
    }


def public_product(p, include_nlc=True):
    cells = p.get("cells")
    if cells is None:
        cells = {"Category": p.get("category"), "Model": p.get("model"),
                 "MRP": p.get("mrp"), "DP": p.get("dp"), "NLC": p.get("nlc")}
    return {"id": p["_id"], "cells": cells}


DEFAULT_COLUMNS = ["Category", "Model", "MRP", "DP", "NLC"]

def public_pricelist(pl, count=0):
    return {
        "id": pl["_id"],
        "name": pl["name"],
        "allowed_user_ids": pl.get("allowed_user_ids", []),
        "count": count,
        "columns": pl.get("columns") or DEFAULT_COLUMNS,
        "model_col": pl.get("model_col") or "Model",
        "price_col": pl.get("price_col") or "DP",
    }


def public_company(c):
    return {"id": c["_id"], "name": c["name"]}


def public_order(o):
    return {
        "id": o["_id"], "dealer_id": o.get("dealer_id"), "dealer_name": o.get("dealer_name"),
        "pricelist_name": o.get("pricelist_name", ""), "note": o.get("note", ""),
        "items": o.get("items", []), "total": o.get("total", 0),
        "status": o.get("status", "pending"), "date": o.get("date"),
        "created_by": o.get("created_by_name"), "created_role": o.get("created_by_role"),
        "bill_no": o.get("bill_no"), "executed_by": o.get("executed_by"), "executed_at": o.get("executed_date"),
    }
