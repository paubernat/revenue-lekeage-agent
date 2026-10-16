"""propose -> apply -> re-investigate shows remediated; rollback; guard rails."""
import pytest

from core import actions, repository
from core.actions import ActionError
from core.investigation import find_finding

SEPT = "F-C-1001-missing_period-2025-09-01"


def test_proposal_writes_nothing_to_ledgers_until_apply():
    p = actions.build_make_good("C-1001", 8000, "Missing September", SEPT)
    assert p.status == "pending"
    assert repository.read_sandbox("invoices.json") == []
    assert find_finding(SEPT).status == "open"


def test_apply_then_reinvestigate_is_remediated_and_rollback_reopens():
    p = actions.build_make_good("C-1001", 8000, "Missing September", SEPT)
    result = actions.apply(p.proposal_id)
    assert result["record"]["invoice_id"] == "INV-MG-001"
    assert find_finding(SEPT).status == "remediated"
    events = [e["event"] for e in repository.read_sandbox("audit_log.json")]
    assert events == ["proposal_created", "applied"]

    actions.rollback()  # last action
    assert repository.read_sandbox("invoices.json") == []
    assert find_finding(SEPT).status == "open"
    # ids are not reused after a rollback
    p2 = actions.build_make_good("C-1001", 8000, "Missing September", SEPT)
    assert actions.apply(p2.proposal_id)["record"]["invoice_id"] == "INV-MG-002"


def test_amount_cannot_exceed_gap():
    with pytest.raises(ActionError, match="exceeds"):
        actions.build_make_good("C-1001", 9000, "too much", SEPT)


def test_no_duplicate_credit_for_m300():
    with pytest.raises(ActionError, match="already remediated"):
        actions.build_credit_memo("I-9123", 2000, "FX", "F-C-1007-A1-currency_mismatch-2025-07-01")


def test_wrong_direction_rejected():
    with pytest.raises(ActionError, match="not an overbilling"):
        actions.build_credit_memo("I-9201", 100, "x", "F-C-1010-underbilled-2025-01-15")


def test_same_draft_is_idempotent_and_reject_works():
    a = actions.build_make_good("C-1001", 8000, "Missing September", SEPT)
    b = actions.build_make_good("C-1001", 8000, "Missing September", SEPT)
    assert a.proposal_id == b.proposal_id
    actions.reject(a.proposal_id)
    with pytest.raises(ActionError, match="not pending"):
        actions.apply(a.proposal_id)


def test_amendment_changes_plan_and_closes_gap():
    f = "F-C-1010-underbilled-2025-01-15"
    p = actions.build_amendment("C-1010", {"total_value": 100000, "entitlements": ["Consulting"]},
                                "Customer bought base plan only", f)
    actions.apply(p.proposal_id)
    assert repository.get_plans()["C-1010"].total_value == 100000
    assert find_finding(f) is None  # no gap any more
    actions.rollback()
    assert repository.get_plans()["C-1010"].total_value == 120000


def test_amendment_rejects_unknown_fields():
    with pytest.raises(ActionError, match="may only contain"):
        actions.build_amendment("C-1010", {"currency": "EUR"}, "x")
