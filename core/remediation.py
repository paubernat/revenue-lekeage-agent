"""After detection: is the problem already fixed, and what fix should be offered?

    delta            = actual - expected            (from the detectors, never changes)
    existing fixes   = credit memos (for delta > 0) or applied make-goods (for delta < 0)
    remaining_delta  = delta minus the existing fixes, never past zero

    status:  remediated            remaining is 0       e.g. I-9123 +2,000, fully covered by M-300
             partially_remediated  some fix, not all    e.g. 5,000 make-good on an 8,000 gap
             open                  no fix yet

The suggested action is then pre-filled from remaining_delta, so the agent proposes exactly the
amount still open: make-good if negative, credit memo if positive.
"""
from core.fx import FxError, fx_convert
from core.models import EPS, CreditMemo, Finding, Invoice, Plan


def net_existing_fixes(f: Finding, plan: Plan, memos: list[CreditMemo], make_goods: list[Invoice]) -> set[str]:
    """Fill f.remaining_delta / f.status / f.existing_remediation.
    Returns the credit memos used, so checks.unmatched_credits can spot memos nothing explains."""
    fixes = _fixes_for(f, memos, make_goods)  # [(record id, amount, currency, date)]
    covered, ids = 0.0, []
    for rid, amount, ccy, on in fixes:
        try:
            covered += fx_convert(amount, ccy, plan.currency, on)["amount"]
            ids.append(rid)
        except FxError as e:
            f.assumptions.append(f"{rid} could not be netted: {e}")

    sign = 1 if f.delta > 0 else -1
    f.existing_remediation = ids
    f.remaining_delta = round(sign * max(abs(f.delta) - covered, 0), 2) + 0.0  # + 0.0 avoids -0.0
    if ids:
        f.status = "remediated" if abs(f.remaining_delta) < EPS else "partially_remediated"
        f.calculation += f"; already covered {covered:g} by {ids} -> remaining {f.remaining_delta:+g}"
    return set(ids) if f.delta > 0 else set()


def _fixes_for(f: Finding, memos: list[CreditMemo], make_goods: list[Invoice]) -> list[tuple]:
    if f.delta > 0:  # overbilled -> credit memos fix it: ours (by finding_id) or older ones on the same invoice
        return [(m.memo_id, m.amount, m.currency, m.issue_date) for m in memos
                if m.finding_id == f.finding_id or (m.finding_id is None and m.invoice_id in f.invoice_ids)]
    if f.delta < 0:  # underbilled -> make-good invoices applied for this finding fix it
        return [(i.invoice_id, i.amount_invoiced, i.currency, i.issue_date) for i in make_goods
                if i.finding_id == f.finding_id]
    return []


def suggest_action(f: Finding, plan: Plan) -> None:
    """Pre-fill the propose_* call for the amount still open. Nothing for fixed or review-only findings."""
    if f.status in ("remediated", "needs_review") or abs(f.remaining_delta) < EPS:
        return
    period = f" for {f.period['start']}..{f.period['end']}" if f.period else ""
    reason = f"{f.type.replace('_', ' ').capitalize()}{period}"
    if f.remaining_delta < 0:
        f.suggested_action = {"tool": "propose_make_good_invoice", "args": {
            "plan_id": plan.plan_id, "amount": abs(f.remaining_delta), "reason": reason, "finding_id": f.finding_id}}
    elif f.invoice_ids:
        f.suggested_action = {"tool": "propose_credit_memo", "args": {
            "invoice_id": f.invoice_ids[0], "amount": f.remaining_delta, "reason": reason,
            "finding_id": f.finding_id}}
