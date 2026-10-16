"""One test per edge case (ids match EDGE_CASES.md). Most run on data_edge_cases/, where each
plan isolates one case: monthly 12,000 USD plans from 2025-10-01, so Oct-Dec are due (1,000 each)."""
from datetime import date

import pytest

import config
from core import actions, checks, repository
from core.actions import ActionError
from core.investigation import find_finding, investigate
from core.models import Finding, Plan
from core.schedule import contracted_schedule

SEPT = "F-C-1001-missing_period-2025-09-01"


@pytest.fixture
def edge(monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", config.DATASETS["Edge cases"])


def plan_findings(plan_id):
    return {f["finding_id"]: f for f in investigate(plan_id)["plans"][0]["findings"]}


def periods(plan_id):
    return {p["start"]: p for p in investigate(plan_id)["plans"][0]["periods"]}


def other(finding_id):
    return next(f for f in investigate("all")["other_findings"] if f["finding_id"] == finding_id)


# ---------------------------------------------------------------- handled before, now tested

def test_ec13_duplicate_invoice(edge):
    f = plan_findings("P-DUP")
    assert list(f) == ["F-P-DUP-duplicate_invoice-E-1304"]
    dup = f["F-P-DUP-duplicate_invoice-E-1304"]
    assert dup["delta"] == 1000 and dup["suggested_action"]["tool"] == "propose_credit_memo"


def test_ec14_out_of_term_invoice(edge):
    assert list(plan_findings("P-OOT")) == ["F-P-OOT-out_of_term-E-1401"]


def test_ec15_notes_contradict_plan(edge):
    assert list(plan_findings("P-NOTES")) == ["F-P-NOTES-plan_inconsistency"]
    assert plan_findings("P-SPLIT") == {}  # numbers without a currency ("600 + 400") are not amounts


def test_ec17_partial_fix_is_partially_remediated():
    p = actions.build_make_good("C-1001", 5000, "Part of September", SEPT)
    actions.apply(p.proposal_id)
    f = find_finding(SEPT)
    assert f.status == "partially_remediated" and f.remaining_delta == -3000
    assert f.suggested_action["args"]["amount"] == 3000


# ---------------------------------------------------------------- new cases

def test_ec18_missing_fx_rate_is_flagged_not_skipped(edge):
    f = plan_findings("P-FX")
    assert list(f) == ["F-P-FX-fx_rate_missing-E-1803"]
    assert f["F-P-FX-fx_rate_missing-E-1803"]["status"] == "needs_review"
    assert periods("P-FX")["2025-12-01"]["status"] == "unverified_missing_fx_rate"  # not "missing"


def test_ec19_split_billing_is_not_a_duplicate(edge):
    assert plan_findings("P-SPLIT") == {}
    assert periods("P-SPLIT")["2025-11-01"]["status"] == "ok"


def test_ec20_early_billing_counts_for_next_period(edge):
    assert plan_findings("P-EARLY") == {}
    assert "early" in periods("P-EARLY")["2025-12-01"]["invoices"][0]


def test_ec21_void_invoice_is_not_counted(edge):
    f = plan_findings("P-VOID")["F-P-VOID-missing_period-2025-12-01"]
    assert f["delta"] == -1000
    assert any("E-2103" in a and "void" in a for a in f["assumptions"])


def test_ec22_unpaid_past_due_is_collection_risk(edge):
    f = plan_findings("P-UNPAID")
    assert list(f) == ["F-P-UNPAID-collection_risk-E-2201"]
    assert f["F-P-UNPAID-collection_risk-E-2201"]["suggested_action"] is None


def test_ec23_refund_is_netted_with_low_confidence(edge):
    f = plan_findings("P-REFUND")["F-P-REFUND-underbilled-2025-11-01"]
    assert f["delta"] == -1000 and f["confidence"] == "low"
    assert any("E-2303" in a and "refund" in a.lower() for a in f["assumptions"])


def test_ec24_orphan_matched_by_normalised_customer_name(edge):
    o = other("F-orphan_invoice-E-2409")
    assert o["alternatives"][0]["candidate_plans"][0]["plan_id"] == "P-NAME"
    gap = plan_findings("P-NAME")["F-P-NAME-missing_period-2025-12-01"]
    assert any("E-2409" in a for a in gap["assumptions"])  # flagged as a possible payment, not assumed


def test_ec25_orphan_matched_by_plan_id_lookalike(edge):
    top = other("F-orphan_invoice-E-2509")["alternatives"][0]["candidate_plans"][0]
    assert top["plan_id"] == "P-TYPO" and "looks like P-TYPO" in top["reasons"][0]


def test_ec26_multi_year_contract_uses_end_date(edge):
    assert plan_findings("P-2YEAR") == {}
    assert [p["expected"] for p in periods("P-2YEAR").values()] == [100000, 100000]


def test_ec27_rounding_remainder_on_last_period(edge):
    plan = repository.get_plans()["P-ROUND"]
    sched = contracted_schedule(plan)
    assert sched[0]["expected"] == 833.33 and sched[-1]["expected"] == 833.37
    assert round(sum(p["expected"] for p in sched), 2) == 10000
    assert plan_findings("P-ROUND") == {}


def test_ec28_unexplained_credit_memo(edge):
    f = plan_findings("P-CREDIT")
    assert list(f) == ["F-P-CREDIT-unmatched_credit-EM-2801"]


def test_ec28_explained_credit_is_not_flagged():
    findings = [f for p in investigate("all")["plans"] for f in p["findings"]]
    assert not [f for f in findings if f["type"] == "unmatched_credit"]  # M-300 explains I-9123


def test_ec29_invalid_records_are_reported_not_fatal(edge):
    result = investigate("all")
    problems = [f["calculation"] for f in result["other_findings"] if f["type"] == "data_quality"]
    assert any("P-BAD" in p and "cadence" in p for p in problems)
    assert any("E-2901" in p and "amount_invoiced" in p for p in problems)
    assert "P-BAD" not in [p["plan"]["plan_id"] for p in result["plans"]]


def test_ec30_duplicate_invoice_id(edge):
    problems = [f["calculation"] for f in investigate("all")["other_findings"] if f["type"] == "data_quality"]
    assert any("E-3001" in p and "duplicate" in p for p in problems)
    assert plan_findings("P-DUPID") == {}  # the copy is ignored, not counted twice


def test_ec31_safety_net_flags_what_no_rule_explains():
    plan = Plan(plan_id="X", customer_name="X", total_value=12000, currency="USD", cadence="Monthly",
                start_date=date(2025, 1, 1))
    one = Finding(finding_id="f1", type="missing_period", plan_id="X", delta=-1000)
    summary, leftover = checks.reconcile(plan, expected_to_date=3000, billed_to_date=1000, findings=[one])
    assert not summary["ok"] and summary["unexplained"] == -1000
    assert leftover.type == "unexplained_difference" and leftover.status == "needs_review"


@pytest.mark.parametrize("dataset", ["Original", "Edge cases"])
def test_ec31_every_plan_reconciles(dataset, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", config.DATASETS[dataset])
    result = investigate("all")
    assert result["summary"]["plans_not_reconciled"] == []
    assert "unexplained_difference" not in result["summary"]["by_type"]


def test_ec32_method_note_is_returned():
    method = investigate("C-1001")["method"]
    assert {"term", "matching", "safety_net", "not_handled"} <= set(method)


# ---------------------------------------------------------------- guard rail

def test_apply_revalidates_when_gap_closed_meanwhile():
    p = actions.build_make_good("C-1001", 8000, "Missing September", SEPT)
    # someone fixes September by another route before the proposal is applied
    repository.append_sandbox("invoices.json", {
        "invoice_id": "INV-MG-999", "plan_id": "C-1001", "customer_name": "ACME Corp",
        "issue_date": "2026-01-02", "amount_invoiced": 8000, "currency": "USD", "kind": "make_good",
        "finding_id": SEPT})
    with pytest.raises(ActionError, match="already remediated"):
        actions.apply(p.proposal_id)
