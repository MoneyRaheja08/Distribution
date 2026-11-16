"""Outstanding + ageing: payments clear the oldest bills first (FIFO)."""
from datetime import date

from app.ledger import bill_breakdown, bucket_for, compute

TODAY = date(2026, 11, 16)
BILLS = [
    {"bill_no": "B3", "date": "2026-11-10", "amount": 3000},   # 6 days old
    {"bill_no": "B1", "date": "2026-07-01", "amount": 1000},   # 138 days
    {"bill_no": "B2", "date": "2026-09-20", "amount": 2000},   # 57 days
]


def test_buckets():
    assert [bucket_for(d) for d in (0, 30, 31, 60, 61, 90, 91)] == \
        ["age_0_30", "age_0_30", "age_31_60", "age_31_60", "age_61_90", "age_61_90", "age_90p"]


def test_no_payments_everything_ages_by_bill_date():
    r = compute(BILLS, [], TODAY)
    assert r["outstanding"] == 6000
    assert r["ageing"] == {"age_0_30": 3000, "age_31_60": 2000, "age_61_90": 0, "age_90p": 1000}
    assert r["last_payment"] is None


def test_payment_clears_oldest_bill_first():
    r = compute(BILLS, [{"amount": 1500, "date": "2026-11-12", "status": "cleared"}], TODAY)
    assert r["outstanding"] == 4500
    assert r["ageing"] == {"age_0_30": 3000, "age_31_60": 1500, "age_61_90": 0, "age_90p": 0}
    assert r["last_payment"] == {"amount": 1500, "date": "2026-11-12"}


def test_bounced_and_unapproved_payments_do_not_count():
    pays = [{"amount": 1000, "date": "2026-11-01", "status": "bounced"},
            {"amount": 1000, "date": "2026-11-02", "status": "cleared", "approved": False},
            {"amount": 400, "date": "2026-10-01", "status": "pending"}]          # a pending cheque DOES count
    r = compute(BILLS, pays, TODAY)
    assert r["outstanding"] == 5600 and r["ageing"]["age_90p"] == 600
    assert r["last_payment"] == {"amount": 400, "date": "2026-10-01"}


def test_overpayment_leaves_nothing_due():
    r = compute(BILLS, [{"amount": 9000, "date": "2026-11-12", "status": "cleared"}], TODAY)
    assert r["outstanding"] == 0 and sum(r["ageing"].values()) == 0


def test_bill_breakdown_matches_compute():
    pays = [{"amount": 1500, "date": "2026-11-12", "status": "cleared"}]
    rows = bill_breakdown(BILLS, pays, TODAY)
    assert [(x["bill_no"], x["unpaid"], x["days"], x["bucket"]) for x in rows] == \
        [("B2", 1500, 57, "age_31_60"), ("B3", 3000, 6, "age_0_30")]
    assert sum(x["unpaid"] for x in rows) == compute(BILLS, pays, TODAY)["outstanding"]


def test_remainder_of_half_a_rupee_or_less_is_ignored():
    r = compute([{"bill_no": "X", "date": "2026-11-01", "amount": 100.4}], [{"amount": 100, "status": "cleared"}], TODAY)
    assert r["outstanding"] == 0
