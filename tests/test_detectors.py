"""The dataset's known anomalies, with exact amounts (as-of 2025-12-31)."""
from datetime import date

from core.investigation import investigate
from core.fx import fx_convert


def findings(plan_id="all", as_of=None):
    r = investigate(plan_id, as_of)
    return {f["finding_id"]: f for p in r["plans"] for f in p["findings"]} | {
        f["finding_id"]: f for f in r["other_findings"]}


def test_acme_missing_months():
    f = findings("C-1001")
    missing = sorted(k for k, v in f.items() if v["type"] == "missing_period")
    assert missing == ["F-C-1001-missing_period-2025-09-01", "F-C-1001-missing_period-2025-11-01",
                       "F-C-1001-missing_period-2025-12-01"]
    assert all(f[k]["delta"] == -8000 and f[k]["confidence"] == "high" for k in missing)
    assert f[missing[0]]["suggested_action"]["tool"] == "propose_make_good_invoice"


def test_as_of_limits_due_periods():
    f = findings("C-1001", as_of=date(2025, 10, 31))
    assert [k for k, v in f.items() if v["type"] == "missing_period"] == ["F-C-1001-missing_period-2025-09-01"]


def test_acme_unpaid_invoice_is_collection_risk():
    risk = findings("C-1001")["F-C-1001-collection_risk-I-9010"]
    assert risk["status"] == "needs_review" and "72 days overdue" in risk["calculation"]


def test_globex_original_term_late_billing_not_false_positive():
    f = findings("C-1007")
    # no "missing Mar-May" + "overbilled June" pair; the orphan is listed because C-1007 is a candidate
    assert list(f) == ["F-C-1007-underbilled-2025-03-01", "F-orphan_invoice-I-9202"]
    finding = f["F-C-1007-underbilled-2025-03-01"]
    assert finding["delta"] == -2500
    assert finding["confidence"] == "medium"
    assert finding["alternatives"][0]["delta"] == -10000  # prorated June stub reading


def test_globex_fx_overbilling_already_remediated_by_m300():
    f = findings("C-1007-A1")
    fx = f["F-C-1007-A1-currency_mismatch-2025-07-01"]
    assert fx["delta"] == 2000 and fx["status"] == "remediated"
    assert fx["existing_remediation"] == ["M-300"] and fx["suggested_action"] is None
    q4 = f["F-C-1007-A1-missing_period-2025-10-01"]
    assert q4["delta"] == -25000
    assert any("I-9202" in a for a in q4["assumptions"])  # orphan payment flagged, not assumed


def test_initech_underbilled_with_amendment_alternative():
    f = findings("C-1010")
    u = f["F-C-1010-underbilled-2025-01-15"]
    assert u["delta"] == -20000
    amend = u["alternatives"][0]["suggested_action"]
    assert amend["tool"] == "propose_plan_amendment"
    assert amend["args"]["change_set"] == {"total_value": 100000, "entitlements": ["Consulting"]}
    assert f["F-C-1010-entitlement_gap-I-9201"]["status"] == "needs_review"


def test_orphan_invoice_candidates():
    o = findings()["F-orphan_invoice-I-9202"]
    assert o["status"] == "needs_review"
    assert o["alternatives"][0]["candidate_plans"][0]["plan_id"] == "C-1007-A1"


def test_summary_totals():
    s = investigate()["summary"]
    assert s["net_underbilled"] == 3 * 8000 + 2500 + 25000 + 20000
    assert s["remediated"] == 1


def test_fx_closest_prior_rate():
    exact = fx_convert(25000, "EUR", "USD", "2025-09-12")
    assert exact["amount"] == 27000 and "note" not in exact
    later = fx_convert(100, "EUR", "USD", "2025-10-01")
    assert later["rate"] == 1.08 and "closest earlier" in later["note"]
    inverse = fx_convert(108, "USD", "EUR", "2025-09-12")
    assert inverse["amount"] == 100
