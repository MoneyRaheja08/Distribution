from datetime import date, datetime

BUCKETS = ("age_0_30", "age_31_60", "age_61_90", "age_90p")


def _parse(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def bucket_for(days: int) -> str:
    if days <= 30:
        return "age_0_30"
    if days <= 60:
        return "age_31_60"
    if days <= 90:
        return "age_61_90"
    return "age_90p"


def compute(bills, payments, today=None):
    """Outstanding + ageing from a dealer's bills and payments.
    Payments clear oldest bills first (FIFO); unpaid remainder ages by bill date."""
    today = today or date.today()
    bills_sorted = sorted(bills, key=lambda b: (b.get("date") or "9999-99-99"))
    def counts(x):
        return x.get("status") != "bounced" and x.get("approved", True)
    pool = sum(float(p["amount"]) for p in payments if counts(p))
    ageing = {k: 0.0 for k in BUCKETS}
    outstanding = 0.0
    for b in bills_sorted:
        amt = float(b.get("amount") or 0)
        paid = min(pool, amt)
        pool -= paid
        unpaid = amt - paid
        if unpaid > 0.5:
            outstanding += unpaid
            d = _parse(b.get("date"))
            days = (today - d).days if d else 0
            ageing[bucket_for(days)] += unpaid
    live = [p for p in payments if counts(p)]
    last = max(live, key=lambda p: p.get("date") or "", default=None)
    return {
        "outstanding": round(outstanding),
        "ageing": {k: round(v) for k, v in ageing.items()},
        "last_payment": ({"amount": last["amount"], "date": last.get("date")} if last else None),
    }


def bill_breakdown(bills, payments, today=None):
    """Per-bill ageing: each still-unpaid bill with its unpaid amount, age and bucket
    (payments applied oldest-first, same FIFO as compute)."""
    today = today or date.today()
    bills_sorted = sorted(bills, key=lambda b: (b.get("date") or "9999-99-99"))

    def counts(x):
        return x.get("status") != "bounced" and x.get("approved", True)

    pool = sum(float(p["amount"]) for p in payments if counts(p))
    out = []
    for b in bills_sorted:
        amt = float(b.get("amount") or 0)
        paid = min(pool, amt)
        pool -= paid
        unpaid = amt - paid
        if unpaid > 0.5:
            d = _parse(b.get("date"))
            days = (today - d).days if d else 0
            out.append({"bill_no": b.get("bill_no"), "date": b.get("date"),
                        "amount": round(amt), "unpaid": round(unpaid),
                        "days": days, "bucket": bucket_for(days)})
    return out
