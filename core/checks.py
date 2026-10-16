"""Checks that look across plans or around the period matching:
orphans, collection risk, unexplained credits, data quality, and the reconciliation safety net."""
import re
from datetime import date

from core.models import CreditMemo, Finding, Invoice, Money, Plan
from core.schedule import per_period_amount, term

EPS = 0.01
UNPAID_STATUSES = {"unpaid", "overdue", "open", "past_due"}
NAME_SUFFIXES = {"ltd", "limited", "inc", "corp", "corporation", "llc", "gmbh", "sa", "plc", "co", "company"}
# Finding types whose delta is part of the period-by-period comparison
PERIOD_TYPES = {"missing_period", "underbilled", "overbilled", "currency_mismatch", "duplicate_invoice"}


def norm_name(name: str) -> str:
    """'GLOBEX, Ltd.' -> 'globex'"""
    words = re.sub(r"[^a-z0-9 ]", " ", name.casefold()).split()
    return " ".join(w for w in words if w not in NAME_SUFFIXES)


def norm_id(plan_id: str) -> str:
    """'c 1001' / 'C1001' -> 'c1001'"""
    return re.sub(r"[^a-z0-9]", "", plan_id.lower())


# ---------------------------------------------------------------- orphans (EC-09, EC-24, EC-25)

def detect_orphans(orphans: list[Invoice], plans: dict[str, Plan]) -> list[Finding]:
    out = []
    for inv in orphans:
        candidates = []
        for p in plans.values():
            same_customer = norm_name(p.customer_name) == norm_name(inv.customer_name)
            id_lookalike = bool(inv.plan_id) and norm_id(inv.plan_id) == norm_id(p.plan_id)
            if not (same_customer or id_lookalike):
                continue
            score, reasons = 0, []
            if id_lookalike:
                score += 3
                reasons.append(f"plan_id '{inv.plan_id}' looks like {p.plan_id}")
            if same_customer:
                score += 1
                reasons.append("same customer" if p.customer_name == inv.customer_name
                               else f"same customer after normalising names ('{inv.customer_name}')")
            t = term(p, plans)
            if t["start"] <= inv.issue_date <= t["end"]:
                score += 2
                reasons.append(f"issue date inside term {t['start']}..{t['end']}")
            if abs(inv.amount_invoiced - per_period_amount(p)) < EPS:
                score += 1
                reasons.append("amount equals per-period amount")
            candidates.append({"plan_id": p.plan_id, "score": score, "reasons": reasons})
        candidates.sort(key=lambda c: -c["score"])
        out.append(Finding(
            plan_id=None, customer_name=inv.customer_name,
            finding_id=f"F-orphan_invoice-{inv.invoice_id}", type="orphan_invoice", invoice_ids=[inv.invoice_id],
            actual=Money(amount=inv.amount_invoiced, currency=inv.currency),
            calculation=f"{inv.invoice_id} has plan_id '{inv.plan_id}' which matches no billing plan",
            evidence=[inv.invoice_id, f"Description: '{inv.description}'"],
            assumptions=["Candidates ranked by plan id, customer, term and amount; attribution needs human "
                         "confirmation"] + ([] if candidates else ["No candidate plan found"]),
            confidence="low", status="needs_review",
            alternatives=[{"candidate_plans": candidates}],
        ))
    return out


def link_orphans(orphan_findings: list[Finding], plan_findings: list[Finding], invoices: dict[str, Invoice]) -> None:
    """An orphan payment may explain an open gap of a candidate plan: say so, don't decide."""
    for o in orphan_findings:
        inv = invoices[o.invoice_ids[0]]
        cands = {c["plan_id"] for c in o.alternatives[0]["candidate_plans"] if c["score"] >= 3}
        for f in plan_findings:
            if (f.plan_id in cands and f.remaining_delta < 0 and f.period
                    and f.period["start"] <= str(inv.issue_date) <= f.period["end"]):
                f.confidence = "medium" if f.confidence == "high" else f.confidence
                f.assumptions.append(f"Orphan invoice {inv.invoice_id} ({inv.amount_invoiced:g} {inv.currency}, "
                                     f"{inv.issue_date}) may be a payment for this period; confirm before "
                                     "issuing a make-good")
                f.alternatives.append({
                    "description": f"If {inv.invoice_id} belongs to {f.plan_id}, only "
                                   f"{max(abs(f.remaining_delta) - inv.amount_invoiced, 0):g} remains to recover",
                })
                o.assumptions.append(f"Would cover {f.finding_id} (gap {abs(f.remaining_delta):g})")


# ---------------------------------------------------------------- collection risk (EC-22)

def collection_risk(plan: Plan, invoices: list[Invoice], as_of: date) -> list[Finding]:
    out = []
    for inv in invoices:
        if inv.status.lower() in UNPAID_STATUSES and inv.due_date and inv.due_date < as_of:
            days = (as_of - inv.due_date).days
            out.append(Finding(
                plan_id=plan.plan_id, customer_name=plan.customer_name,
                finding_id=f"F-{plan.plan_id}-collection_risk-{inv.invoice_id}", type="collection_risk",
                invoice_ids=[inv.invoice_id], actual=Money(amount=inv.amount_invoiced, currency=inv.currency),
                calculation=f"{inv.invoice_id} ({inv.amount_invoiced:g} {inv.currency}) was due {inv.due_date} and "
                            f"is still '{inv.status}' as of {as_of} ({days} days overdue)",
                evidence=[inv.invoice_id], confidence="high", status="needs_review",
                assumptions=["Billed correctly but not collected: follow up with collections, no billing fix needed"],
            ))
    return out


# ---------------------------------------------------------------- unexplained credits (EC-28)

def unmatched_credits(memos: list[CreditMemo], used_memo_ids: set[str], plans: dict[str, Plan],
                      invoices: dict[str, Invoice]) -> list[Finding]:
    """A credit memo that no overbilling explains silently reduces revenue."""
    out = []
    for m in memos:
        if m.memo_id in used_memo_ids:
            continue
        plan = plans.get(m.plan_id)
        inv = invoices.get(m.invoice_id)
        out.append(Finding(
            plan_id=m.plan_id, customer_name=plan.customer_name if plan else "",
            finding_id=f"F-{m.plan_id}-unmatched_credit-{m.memo_id}", type="unmatched_credit",
            invoice_ids=[m.invoice_id], actual=Money(amount=-m.amount, currency=m.currency), delta=0,
            calculation=f"{m.memo_id} credits {m.amount:g} {m.currency} on {m.invoice_id} "
                        f"({'billed ' + format(inv.amount_invoiced, 'g') + ' ' + inv.currency if inv else 'unknown invoice'}),"
                        f" but no overbilling on that invoice explains it",
            evidence=[m.memo_id, m.invoice_id, f"Reason given: '{m.reason}'"], confidence="medium",
            status="needs_review",
            assumptions=["Could be a legitimate goodwill/SLA credit; if not, it is revenue given away"],
        ))
    return out


# ---------------------------------------------------------------- data quality (EC-29, EC-30)

def data_quality(issues: list[dict]) -> list[Finding]:
    return [Finding(
        plan_id=None, finding_id=f"F-data_quality-{i['file'].split('.')[0]}-{i['record_id']}-{n}",
        type="data_quality", calculation=f"{i['file']} record {i['record_id']}: {i['problem']}",
        evidence=[i["file"]], confidence="high", status="needs_review",
        assumptions=["Ignored records are not part of the analysis until the data is fixed"],
    ) for n, i in enumerate(issues, 1)]


# ---------------------------------------------------------------- safety net (EC-31)

def reconcile(plan: Plan, expected_to_date: float, billed_to_date: float,
              findings: list[Finding]) -> tuple[dict, Finding | None]:
    """Independent check: (billed - expected) for the plan must equal the sum of the period
    findings' deltas. Anything left over was not explained by any rule and needs review."""
    gap = round(billed_to_date - expected_to_date, 2)
    explained = round(sum(f.delta for f in findings if f.type in PERIOD_TYPES), 2)
    unexplained = round(gap - explained, 2)
    summary = {"expected_to_date": round(expected_to_date, 2), "billed_to_date": round(billed_to_date, 2),
               "gap": gap, "explained_by_findings": explained, "unexplained": unexplained,
               "ok": abs(unexplained) < EPS}
    if summary["ok"]:
        return summary, None
    return summary, Finding(
        plan_id=plan.plan_id, customer_name=plan.customer_name,
        finding_id=f"F-{plan.plan_id}-unexplained_difference", type="unexplained_difference",
        expected=Money(amount=expected_to_date, currency=plan.currency),
        actual=Money(amount=billed_to_date, currency=plan.currency), delta=0,
        calculation=f"Billed {billed_to_date:g} - expected {expected_to_date:g} = {gap:+g}, but findings explain "
                    f"{explained:+g} -> {unexplained:+g} {plan.currency} unexplained",
        confidence="low", status="needs_review",
        assumptions=["No rule explains this difference; inspect the invoices with query_invoices"],
    )
