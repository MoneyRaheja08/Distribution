from .db import db


async def cost_override_map(company):
    """Manual NLC (cost per unit) per (BRAND, model). Used only as a fallback when no
    purchase cost exists — a real purchase always wins."""
    m = {}
    async for o in db.cost_overrides.find({"company_id": company}):
        b = (o.get("brand") or "—").strip().upper()
        m[(b, o.get("model") or "—")] = o.get("nlc", 0) or 0
    return m
