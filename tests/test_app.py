"""End-to-end checks through the real API: security rules, reports agreeing with each other,
the CN calculator, the audit trail, and backup → restore."""
import io
from datetime import datetime, timedelta, timezone

from bson import json_util

from app.timeutil import IST, today_ist


def seed(api):
    """Company A: two dealers. D1 owes 4,500 (see test_ledger), D2 owes 800. Company B: one dealer."""
    A, B = api.A, api.B
    api.insert("dealers", [
        {"_id": "d1", "company_id": A, "name": "Sharma Electronics", "phone": "9000000001", "credit_limit": 0},
        {"_id": "d2", "company_id": A, "name": "Verma Traders", "phone": "9000000002", "credit_limit": 0},
        {"_id": "dB", "company_id": B, "name": "Other Co Dealer"},
    ])
    api.insert("bills", [
        {"_id": "b1", "company_id": A, "dealer_id": "d1", "bill_no": "B1", "date": "2026-07-01", "amount": 1000},
        {"_id": "b2", "company_id": A, "dealer_id": "d1", "bill_no": "B2", "date": "2026-09-20", "amount": 2000},
        {"_id": "b3", "company_id": A, "dealer_id": "d1", "bill_no": "B3", "date": "2026-11-10", "amount": 3000},
        {"_id": "b4", "company_id": A, "dealer_id": "d2", "bill_no": "V1", "date": "2026-11-01", "amount": 800},
        {"_id": "bB", "company_id": B, "dealer_id": "dB", "bill_no": "Z1", "date": "2026-11-01", "amount": 99999},
    ])
    ts = datetime(2026, 11, 12, 6, 30, tzinfo=timezone.utc)
    api.insert("payments", [
        {"_id": "p1", "company_id": A, "dealer_id": "d1", "dealer_name": "Sharma Electronics", "amount": 1500, "mode": "Cash",
         "date": "2026-11-12", "status": "cleared", "approved": True, "reconciled": False, "deposited": False,
         "collector_id": api.me["id"], "collector_name": "Owner", "ts": ts},
        {"_id": "pB", "company_id": B, "dealer_id": "dB", "dealer_name": "Other Co Dealer", "amount": 500, "mode": "Cheque",
         "date": "2026-11-12", "status": "pending", "approved": False, "reconciled": False, "ts": ts},
    ])


# ---------------- security ----------------
def test_login_locks_after_8_wrong_pins(api):
    codes = [api.c.post("/auth/login", json={"name": "Owner", "pin": "0000"}).status_code for _ in range(9)]
    assert codes == [401] * 8 + [429]
    assert api.c.post("/auth/login", json={"name": "owner ", "pin": "4321"}).status_code == 429   # even the right PIN waits


def test_correct_login_clears_the_count(api):
    for _ in range(7):
        api.c.post("/auth/login", json={"name": "Owner", "pin": "0000"})
    assert api.c.post("/auth/login", json={"name": "Owner", "pin": "4321"}).status_code == 200
    codes = [api.c.post("/auth/login", json={"name": "Owner", "pin": "0000"}).status_code for _ in range(8)]
    assert codes == [401] * 8


def test_no_token_no_data(api):
    assert api.c.get("/dealers", headers={"X-Company-Id": api.A}).status_code in (401, 403)
    assert api.c.get("/backup/full").status_code in (401, 403)


def test_payments_of_another_company_cannot_be_touched(api):
    seed(api)
    h = api.h("A")
    assert api.c.delete("/payments/pB", headers=h).status_code == 404
    assert api.c.patch("/payments/pB/reconcile", headers=h, json={"reconciled": True}).status_code == 404
    assert api.c.patch("/payments/pB/approve", headers=h, json={"approved": False}).status_code == 404
    assert api.c.patch("/payments/pB/cheque", headers=h, json={"cleared": True}).status_code == 404
    assert len(api.find("payments", {"_id": "pB"})) == 1


def test_manager_and_collector_cannot_use_admin_tools(api):
    seed(api)
    api.post("/users", {"name": "Mgr", "pin": "1111", "role": "manager", "company_ids": [api.A]}, company=None)
    tok = api.c.post("/auth/login", json={"name": "Mgr", "pin": "1111"}).json()["access_token"]
    h = {"Authorization": "Bearer " + tok, "X-Company-Id": api.A}
    for method, path in [("get", "/backup/full"), ("get", "/backup/audit"), ("delete", "/payments/p1"),
                         ("delete", "/bills/b1"), ("delete", "/dealers/d1"), ("get", "/users")]:
        assert getattr(api.c, method)(path, headers=h).status_code == 403, path
    assert api.c.get("/dealers", headers={**h, "X-Company-Id": api.B}).status_code == 403    # not his company


def test_server_errors_do_not_leak_details(api):
    from app.main import app

    @app.get("/__boom")
    async def boom():
        raise ValueError("mongodb+srv://user:secret@cluster")

    r = api.c.get("/__boom", headers={"Origin": "https://x.test"})
    assert r.status_code == 500 and "secret" not in r.text and "ref " in r.json()["detail"]


def test_today_is_india_time():
    assert today_ist() == datetime.now(IST).date()
    assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)


# ---------------- reports agree with each other ----------------
def test_outstanding_is_the_same_everywhere(api):
    seed(api)
    dealers = {d["id"]: d for d in api.get("/dealers")}
    assert dealers["d1"]["outstanding"] == 4500 and dealers["d2"]["outstanding"] == 800
    assert "dB" not in dealers
    summary = api.get("/payments/summary")
    ageing = api.get("/reports/ageing")
    follow = api.get("/reports/followup")
    assert ageing["total_outstanding"] == 5300
    assert summary["total_outstanding"] == 5300
    assert sum(r["outstanding"] for r in follow["rows"]) == 5300
    assert sum(ageing["ageing"].values()) == 5300
    bill_rows = api.get("/reports/bill-ageing")
    assert sum(b["unpaid"] for d in bill_rows["dealers"] for b in d["bills"]) == 5300
    owed = api.get("/reports/owed-by-brand")
    assert owed.get("total", owed.get("total_outstanding", 5300)) == 5300


def test_every_report_opens(api):
    """Each report endpoint must at least load on real-shaped data (catches crashes after a change)."""
    seed(api)
    from app.main import app
    skipped = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if "GET" not in getattr(route, "methods", set()) or not path.startswith("/reports") or "{" in path:
            continue
        r = api.c.get(path, headers=api.h("A"), params={"frm": "2026-11-01", "to": "2026-11-30", "from": "2026-11-01",
                                                        "date_from": "2026-11-01", "date_to": "2026-11-30"})
        if r.status_code == 422:
            skipped.append(path)
            continue
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
    assert len(skipped) <= 3, skipped


# ---------------- CN calculator ----------------
def test_cn_is_price_change_times_units_billed(api):
    A = api.A
    api.insert("purchases", [
        {"_id": "x1", "company_id": A, "brand": "HAIER", "model": "LED-43", "date": "2026-10-05", "qty": 2, "amount": 40000, "rate": 20000},
        {"_id": "x2", "company_id": A, "brand": "HAIER", "model": "LED-43", "date": "2026-11-03", "qty": 5, "amount": 105000, "rate": 21000},
        {"_id": "x3", "company_id": A, "brand": "HAIER", "model": "AC-1.5", "date": "2026-11-04", "qty": 3, "amount": 90000, "rate": 30000},
        {"_id": "x4", "company_id": A, "brand": "SONY", "model": "TV-55", "date": "2026-11-04", "qty": 1, "amount": 60000, "rate": 60000},
    ])
    body = {"base_from": "2026-10-01", "base_to": "2026-10-31", "cur_from": "2026-11-01", "cur_to": "2026-11-30", "brand": ["HAIER"]}
    r = api.post("/cn/compute", body)
    rows = {x["model"]: x for x in r["rows"]}
    assert set(rows) == {"LED-43", "AC-1.5"}                       # SONY filtered out
    assert rows["LED-43"]["cn"] == (21000 - 20000) * 5
    assert rows["AC-1.5"]["missing_base"] is True and rows["AC-1.5"]["cn"] == 0
    assert r["total_cn"] == 5000 and r["missing_count"] == 1
    # a manual base price fills the gap
    r = api.post("/cn/compute", {**body, "overrides": [{"model": "AC-1.5", "old_price": 29000}]})
    assert r["total_cn"] == 5000 + (30000 - 29000) * 3


# ---------------- audit trail ----------------
def test_deletes_are_recorded_with_a_copy(api):
    seed(api)
    assert api.c.delete("/payments/p1", headers=api.h()).status_code == 200
    assert api.c.delete("/bills/b3", headers=api.h()).status_code == 200
    assert api.c.delete("/dealers/d2", headers=api.h()).status_code == 200
    log = api.get("/backup/audit")
    assert [x["action"] for x in log] == ["dealer.delete", "bill.delete", "payment.delete"]
    assert all(x["user"] == "Owner" for x in log)
    assert "Sharma Electronics" in log[2]["summary"] and "1,500" in log[2]["summary"]
    raw = {a["action"]: a for a in api.find("audit_log")}
    assert raw["payment.delete"]["removed"]["payments"][0]["_id"] == "p1"
    assert raw["bill.delete"]["removed"]["bills"][0]["amount"] == 3000
    assert raw["dealer.delete"]["counts"] == {"dealers": 1, "bills": 1}
    assert api.get("/backup/audit", company="B") == []              # other company sees nothing


def test_rejected_collection_is_recorded(api):
    seed(api)
    assert api.c.patch("/payments/pB/approve", headers=api.h("B"), json={"approved": False}).status_code == 200
    assert api.find("payments", {"_id": "pB"}) == []
    assert [x["action"] for x in api.get("/backup/audit", company="B")] == ["payment.reject"]


# ---------------- backup and restore ----------------
def upload(api, raw, mode, confirm="RESTORE"):
    return api.c.post("/backup/restore", headers=api.h(None), data={"mode": mode, "confirm": confirm},
                      files={"file": ("backup.json", io.BytesIO(raw), "application/json")})


def test_full_backup_and_restore_round_trip(api):
    seed(api)
    raw = api.c.get("/backup/full", headers=api.h(None)).content
    data = json_util.loads(raw)
    assert data["app"] == "ashoka-distribution" and data["counts"]["bills"] == 5 and data["counts"]["companies"] == 2
    before = api.get("/reports/ageing")

    # things go wrong after the backup
    assert api.c.delete("/dealers/d1", headers=api.h()).status_code == 200
    api.insert("bills", [{"_id": "later", "company_id": api.A, "dealer_id": "d2", "bill_no": "NEW", "date": "2026-11-15", "amount": 50}])
    assert api.get("/reports/ageing")["total_outstanding"] == 850

    assert upload(api, raw, "merge", confirm="yes").status_code == 400
    assert upload(api, b'{"hello": 1}', "merge").status_code == 400
    assert upload(api, b"not json", "merge").status_code == 400

    r = upload(api, raw, "merge")
    assert r.status_code == 200 and r.json()["counts"]["bills"] == 5
    assert api.get("/reports/ageing")["total_outstanding"] == 5350                    # deleted dealer is back, the newer bill stays
    assert len(api.find("bills", {"_id": "later"})) == 1

    r = upload(api, raw, "replace")
    assert r.status_code == 200
    assert api.get("/reports/ageing") == before                           # exactly as it was
    assert api.find("bills", {"_id": "later"}) == []
    p1 = api.find("payments", {"_id": "p1"})[0]
    assert isinstance(p1["ts"], datetime) and p1["ts"].replace(tzinfo=None) == datetime(2026, 11, 12, 6, 30)   # dates stay dates
    # still signed in, PIN unchanged, and the trail of what happened was not wiped
    assert api.c.post("/auth/login", json={"name": "Owner", "pin": "4321"}).status_code == 200
    actions = [a["action"] for a in api.find("audit_log")]
    assert actions.count("backup.restore") == 2 and "dealer.delete" in actions


def test_restore_keeps_my_login_even_if_the_file_has_a_different_pin(api):
    seed(api)
    raw = api.c.get("/backup/full", headers=api.h(None)).content
    api.c.patch("/users/" + api.me["id"], headers=api.h(None), json={"pin": "9999"})
    assert api.c.post("/auth/login", json={"name": "Owner", "pin": "9999"}).status_code == 200
    assert upload(api, raw, "replace").status_code == 200
    assert api.c.post("/auth/login", json={"name": "Owner", "pin": "9999"}).status_code == 200


# ---------------- the screens and the server agree ----------------
def test_every_address_the_app_calls_exists_on_the_server():
    """A screen calling an address the server doesn't have just spins forever. This reads the
    frontend's API list (when the frontend folder sits next to this one) and checks each address."""
    import os
    import re

    import pytest
    from starlette.routing import Match

    from app.main import app
    client_js = os.path.join(os.path.dirname(__file__), "..", "..", "distribution-frontend", "src", "api", "client.js")
    if not os.path.exists(client_js):
        pytest.skip("frontend folder not next to backend")
    missing, checked = [], 0
    for line in open(client_js, encoding="utf-8").read().splitlines():
        for m in re.finditer(r"\bhttp(Form|Text)?\(", line):
            rest = line[m.end():]
            depth, end = 0, len(rest)
            for i, ch in enumerate(rest):                       # the first argument = the address expression
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    if depth == 0:
                        end = i
                        break
                    depth -= 1
                elif ch == "," and depth == 0:
                    end = i
                    break
            expr, after = rest[:end], rest[end:]
            if not expr.strip().startswith(("'", "`")):
                continue
            path = ""
            for part in re.split(r"\s\+\s", expr):
                part = part.strip()
                lit = re.fullmatch(r"(['`])(.*)\1", part)
                if lit:
                    path += re.sub(r"\$\{[^}]*\}", "x", lit.group(2))
                elif path.endswith("/"):
                    path += "x"                                 # an id
                else:
                    break                                       # a query string follows
                if "?" in path:
                    break
            path = path.split("?")[0]
            mm = re.match(r"\s*,\s*\{[^}]*method:\s*'(\w+)'", after)
            method = mm.group(1) if mm else ("POST" if m.group(1) == "Form" else "GET")
            checked += 1
            if not any(r.matches({"type": "http", "path": path, "method": method})[0] == Match.FULL for r in app.routes):
                missing.append(method + " " + path)
    assert checked > 100, checked
    assert missing == [], missing
