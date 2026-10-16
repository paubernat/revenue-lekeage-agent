"""Compare expected billing (schedule.py) with actual invoices and produce Findings.

Deterministic: same data + same as_of => same findings with the same ids. Ambiguity is
never hidden. It goes into `assumptions`, `confidence` and `alternatives` so the agent
can ask the human.
"""
import re
from collections import defaultdict
from datetime import date

import config
from core import repository
from core.fx import FxError, fx_convert
from core.models import PERIODS_PER_YEAR, CreditMemo, Finding, Invoice, Money, Plan
from core.schedule import effective_plan_id, expected_periods, per_period_amount, term

EPS = 0.01


def _to_plan_ccy(amount: float, ccy: str, plan: Plan, on: date) -> dict:
    try:
        return fx_convert(amount, ccy, plan.currency, on)
    except FxError as e:
        return {"amount": None, "error": str(e)}


def _period_label(p: dict) -> dict:
    return {"start": str(p["start"]), "end": str(p["end"])}


def _contains(p: dict, d: date) -> bool:
    return p["start"] <= d <= p["end"]


# ---------------------------------------------------------------- per plan

def analyze_plan(plan: Plan, plans: dict[str, Plan], invoices: list[Invoice], as_of: date) -> dict:
    t = term(plan, plans)
    periods = expected_periods(plan, plans, as_of)
    for p in periods:
        p["invoices"] = []  # list of (invoice, converted dict, late: bool)
    findings: list[Finding] = []
    base = dict(plan_id=plan.plan_id, customer_name=plan.customer_name)
    term_note = (f"Term {t['start']}..{t['end']} (12 months from start_date"
                 + (f", cut short because {t['superseded_by']} supersedes it" if t["superseded_by"] else "") + ")")

    # 1. Assign each invoice to a billing period
    for inv in sorted(invoices, key=lambda i: i.issue_date):
        conv = _to_plan_ccy(inv.amount_invoiced, inv.currency, plan, inv.issue_date)
        if not (t["start"] <= inv.issue_date <= t["end"]):
            findings.append(Finding(
                **base, finding_id=f"F-{plan.plan_id}-out_of_term-{inv.invoice_id}", type="out_of_term",
                invoice_ids=[inv.invoice_id], actual=Money(amount=inv.amount_invoiced, currency=inv.currency),
                calculation=f"{inv.invoice_id} issued {inv.issue_date}, outside term {t['start']}..{t['end']}",
                evidence=[inv.invoice_id], assumptions=[term_note], confidence="low", status="needs_review",
            ))
            continue
        home = next(p for p in periods if _contains(p, inv.issue_date))
        open_earlier = [p for p in periods if p["due"] and not p["partial"] and not p["invoices"]
                        and p["start"] < home["start"]]
        if home["partial"] or home["invoices"]:
            if open_earlier:  # most likely late billing of an earlier, unbilled period
                open_earlier[0]["invoices"].append((inv, conv, True))
                continue
            if home["invoices"]:
                dup_of = home["invoices"][0][0]
                findings.append(Finding(
                    **base, finding_id=f"F-{plan.plan_id}-duplicate_invoice-{inv.invoice_id}",
                    type="duplicate_invoice", invoice_ids=[inv.invoice_id, dup_of.invoice_id],
                    period=_period_label(home), expected=Money(amount=0, currency=plan.currency),
                    actual=Money(amount=conv["amount"] or 0, currency=plan.currency),
                    delta=conv["amount"] or 0,
                    calculation=f"Period {home['start']}..{home['end']} already billed by {dup_of.invoice_id}; "
                                f"{inv.invoice_id} adds {conv['amount']} {plan.currency}",
                    evidence=[inv.invoice_id, dup_of.invoice_id], confidence="medium",
                    assumptions=["One invoice per billing period"],
                ))
                continue
        home["invoices"].append((inv, conv, False))

    # 2. Compare each period's expected amount with what was billed
    for p in periods:
        label = _period_label(p)
        key = f"{plan.plan_id}-{{}}-{p['start']}"
        if not p["invoices"]:
            if p["due"] and not p["partial"]:
                findings.append(Finding(
                    **base, finding_id="F-" + key.format("missing_period"), type="missing_period",
                    period=label, expected=Money(amount=p["expected"], currency=plan.currency),
                    actual=Money(amount=0, currency=plan.currency), delta=-p["expected"],
                    calculation=f"{plan.total_value:g} {plan.currency} / {PERIODS_PER_YEAR[plan.cadence]} "
                                f"{plan.cadence.lower()} periods = {p['expected']:g} expected; no invoice issued in "
                                f"{p['start']}..{p['end']} -> gap {p['expected']:g}",
                    evidence=[f"No {plan.plan_id} invoice dated {p['start']}..{p['end']}"],
                    assumptions=[term_note, f"Period counted as due because it started on or before as-of {as_of}"],
                ))
            continue
        for inv, conv, late in p["invoices"]:
            if conv["amount"] is None:
                continue
            delta = round(conv["amount"] - p["expected"], 2)
            if abs(delta) < EPS:
                continue
            ccy_differs = inv.currency != plan.currency
            ftype = "currency_mismatch" if ccy_differs else ("underbilled" if delta < 0 else "overbilled")
            calc = f"Expected {p['expected']:g} {plan.currency}; "
            calc += (f"{inv.invoice_id} billed {inv.amount_invoiced:g} {inv.currency} -> {conv['calculation']}; "
                     if ccy_differs else f"{inv.invoice_id} billed {inv.amount_invoiced:g} {plan.currency}; ")
            calc += f"delta {delta:+g} {plan.currency}"
            assumptions = [term_note]
            confidence = "high"
            if ccy_differs:
                assumptions.append(f"Invoice in {inv.currency} but plan in {plan.currency}; converted at "
                                   f"{conv['rate']} ({conv['rate_date']})")
                if conv.get("note"):
                    assumptions.append(conv["note"])
            if late:
                confidence = "medium"
                assumptions.append(f"{inv.invoice_id} (issued {inv.issue_date}) assumed to be late billing for "
                                   f"{p['start']}..{p['end']}, the earliest unbilled period")
            findings.append(Finding(
                **base, finding_id="F-" + key.format(ftype), type=ftype, invoice_ids=[inv.invoice_id],
                period=label, expected=Money(amount=p["expected"], currency=plan.currency),
                actual=Money(amount=conv["amount"], currency=plan.currency), delta=delta,
                calculation=calc, evidence=[inv.invoice_id], assumptions=assumptions, confidence=confidence,
            ))

    # 3. Partial stub at the end of a superseded term: prorated billing is an alternative reading
    for p in periods:
        if p["partial"] and p["due"] and not p["invoices"]:
            stub = (f"Stub {p['start']}..{p['end']} before {t['superseded_by']} took over: if billed prorated, "
                    f"{p['expected']:g} {plan.currency} more is due")
            prior = [f for f in findings if f.delta < 0]
            if prior:
                f = prior[-1]
                f.confidence = "medium"
                f.assumptions.append(f"Stub period {p['start']}..{p['end']} treated as not billable (superseded)")
                f.alternatives.append({
                    "description": stub, "delta": round(f.delta - p["expected"], 2),
                    "calculation": f"{f.delta:g} - {p['expected']:g} = {round(f.delta - p['expected'], 2):g}",
                })
            else:
                findings.append(Finding(
                    **base, finding_id=f"F-{plan.plan_id}-missing_period-{p['start']}", type="missing_period",
                    period=_period_label(p), expected=Money(amount=p["expected"], currency=plan.currency),
                    actual=Money(amount=0, currency=plan.currency), delta=-p["expected"],
                    calculation=stub, assumptions=[term_note, "Prorated by whole months"], confidence="low",
                ))

    findings += _entitlement_gaps(plan, invoices, findings)
    findings += _plan_inconsistency(plan)

    return {
        "plan": plan.model_dump(mode="json"),
        "term": {k: str(v) if v else None for k, v in t.items()},
        "per_period_amount": per_period_amount(plan),
        "periods": [_period_row(p, plan) for p in periods],
        "findings": findings,
    }


def _period_row(p: dict, plan: Plan) -> dict:
    billed = round(sum(c["amount"] or 0 for _, c, _ in p["invoices"]), 2)
    if not p["due"]:
        status = "not_due_yet"
    elif not p["invoices"]:
        status = "partial_stub_unbilled" if p["partial"] else "missing"
    elif abs(billed - p["expected"]) < EPS:
        status = "ok"
    else:
        status = "underbilled" if billed < p["expected"] else "overbilled"
    return {
        **_period_label(p), "expected": p["expected"], "currency": plan.currency, "billed_in_plan_ccy": billed,
        "invoices": [f"{i.invoice_id} ({i.amount_invoiced:g} {i.currency}, {i.issue_date}{', late' if late else ''})"
                     for i, _, late in p["invoices"]],
        "partial": p["partial"], "status": status,
    }


def _entitlement_gaps(plan: Plan, invoices: list[Invoice], findings: list[Finding]) -> list[Finding]:
    """Triggered only when the plan notes ask to watch entitlement coverage."""
    if "entitlement" not in plan.notes.lower():
        return []
    out = []
    for inv in invoices:
        missing = [e for e in plan.entitlements if e.lower() not in inv.description.lower()]
        if not missing:
            continue
        out.append(Finding(
            plan_id=plan.plan_id, customer_name=plan.customer_name,
            finding_id=f"F-{plan.plan_id}-entitlement_gap-{inv.invoice_id}", type="entitlement_gap",
            invoice_ids=[inv.invoice_id],
            calculation=f"Plan entitlements {plan.entitlements}; {inv.invoice_id} description "
                        f"'{inv.description}' does not mention {missing}",
            evidence=[inv.invoice_id, f"Plan notes: '{plan.notes}'"], confidence="low", status="needs_review",
            assumptions=["Inferred from free-text description; intent must be confirmed by a human"],
        ))
        # The same invoice is likely underbilled: either recover the money or fix the contract.
        # If the notes name a specific entitlement, that is the one most likely dropped.
        named = [e for e in missing if e.lower() in plan.notes.lower()] or missing
        for f in findings:
            if inv.invoice_id in f.invoice_ids and f.delta < 0:
                f.confidence = "medium"
                f.assumptions.append(f"{inv.invoice_id} is described as '{inv.description}' without {missing}: "
                                     "the customer may have bought less than the plan says")
                f.alternatives.append({
                    "description": f"If the customer intentionally dropped {named}, amend the plan instead of "
                                   "invoicing the difference",
                    "suggested_action": {"tool": "propose_plan_amendment", "args": {
                        "plan_id": plan.plan_id,
                        "change_set": {"total_value": round(plan.total_value + f.delta, 2),
                                       "entitlements": [e for e in plan.entitlements if e not in named]},
                        "reason": f"Align plan with what was actually sold ({inv.invoice_id})",
                        "finding_id": f.finding_id}},
                })
    return out


def _plan_inconsistency(plan: Plan) -> list[Finding]:
    """Amounts written in the notes that contradict total_value / cadence."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}|[A-Z]-\d+(-A\d+)?", "", plan.notes)
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text) if float(n) >= 100]
    expected = per_period_amount(plan)
    bad = [n for n in numbers if abs(n - expected) > EPS and abs(n - plan.total_value) > EPS]
    if not bad:
        return []
    return [Finding(
        plan_id=plan.plan_id, customer_name=plan.customer_name,
        finding_id=f"F-{plan.plan_id}-plan_inconsistency", type="plan_inconsistency",
        calculation=f"Notes mention {bad}; plan says {plan.total_value:g}/{plan.cadence} = {expected:g} per period",
        evidence=[f"Plan notes: '{plan.notes}'"], confidence="low", status="needs_review",
    )]


# ---------------------------------------------------------------- orphans

def detect_orphans(orphans: list[Invoice], plans: dict[str, Plan], as_of: date) -> list[Finding]:
    out = []
    for inv in orphans:
        candidates = []
        for p in plans.values():
            if p.customer_name.casefold() != inv.customer_name.casefold():
                continue
            t = term(p, plans)
            reasons = ["same customer"]
            score = 1
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
            assumptions=["Candidates ranked by customer, term and amount; attribution needs human confirmation"],
            confidence="low", status="needs_review",
            alternatives=[{"candidate_plans": candidates}],
        ))
    return out


# ---------------------------------------------------------------- remediation

def _apply_remediation(f: Finding, plan: Plan | None, memos: list[CreditMemo], make_goods: list[Invoice]) -> None:
    """Net existing fixes (data credit memos like M-300, or sandbox actions) against the finding."""
    covered, ids = 0.0, []
    if plan and f.delta > 0:
        for m in memos:
            if m.finding_id == f.finding_id or (m.finding_id is None and m.invoice_id in f.invoice_ids):
                covered += fx_convert(m.amount, m.currency, plan.currency, m.issue_date)["amount"]
                ids.append(m.memo_id)
    elif plan and f.delta < 0:
        for mg in make_goods:
            if mg.finding_id == f.finding_id:
                covered += fx_convert(mg.amount_invoiced, mg.currency, plan.currency, mg.issue_date)["amount"]
                ids.append(mg.invoice_id)
    f.existing_remediation = ids
    sign = 1 if f.delta > 0 else -1
    f.remaining_delta = round(sign * max(abs(f.delta) - covered, 0), 2) + 0.0  # + 0.0 avoids -0.0
    if ids:
        f.status = "remediated" if abs(f.remaining_delta) < EPS else "partially_remediated"
        f.calculation += f"; already covered {covered:g} by {ids} -> remaining {f.remaining_delta:+g}"


def _suggest(f: Finding, plan: Plan | None) -> None:
    if f.status in ("remediated", "needs_review") or not plan or abs(f.remaining_delta) < EPS:
        return
    period = f" for {f.period['start']}..{f.period['end']}" if f.period else ""
    if f.remaining_delta < 0:
        f.suggested_action = {"tool": "propose_make_good_invoice", "args": {
            "plan_id": plan.plan_id, "amount": abs(f.remaining_delta),
            "reason": f"{f.type.replace('_', ' ').capitalize()}{period}", "finding_id": f.finding_id}}
    elif len(f.invoice_ids) >= 1:
        f.suggested_action = {"tool": "propose_credit_memo", "args": {
            "invoice_id": f.invoice_ids[0], "amount": f.remaining_delta,
            "reason": f"{f.type.replace('_', ' ').capitalize()}{period}", "finding_id": f.finding_id}}


def _link_orphans(orphan_findings: list[Finding], plan_findings: list[Finding], invoices: dict[str, Invoice]) -> None:
    """An orphan payment may explain an open gap of a candidate plan: say so, don't decide."""
    for o in orphan_findings:
        inv = invoices[o.invoice_ids[0]]
        cands = {c["plan_id"] for c in o.alternatives[0]["candidate_plans"] if c["score"] >= 3}
        for f in plan_findings:
            if (f.plan_id in cands and f.remaining_delta < 0 and f.period
                    and f.period["start"] <= str(inv.issue_date) <= f.period["end"]):
                f.confidence = "medium" if f.confidence == "high" else f.confidence
                f.assumptions.append(f"Orphan invoice {inv.invoice_id} ({inv.amount_invoiced:g} {inv.currency}, "
                                     f"{inv.issue_date}) may be a partial payment for this period; confirm before "
                                     "issuing a make-good")
                f.alternatives.append({
                    "description": f"If {inv.invoice_id} belongs to {f.plan_id}, only "
                                   f"{abs(f.remaining_delta) - inv.amount_invoiced:g} remains to recover",
                })
                o.assumptions.append(f"Would partially cover {f.finding_id} (gap {abs(f.remaining_delta):g})")


# ---------------------------------------------------------------- entry point

def investigate(plan_id: str = "all", as_of: date | None = None) -> dict:
    as_of = as_of or config.get_as_of()
    plans = repository.get_plans()
    if plan_id != "all" and plan_id not in plans:
        raise KeyError(f"Unknown plan_id '{plan_id}'. Known: {sorted(plans)}")
    invoices = repository.get_invoices()
    memos = repository.get_credit_memos()

    regular = [i for i in invoices if i.kind == "regular"]
    make_goods = [i for i in invoices if i.kind == "make_good"]
    orphans = [i for i in regular if not i.plan_id or i.plan_id not in plans]
    by_plan: dict[str, list[Invoice]] = defaultdict(list)
    for inv in regular:
        if inv not in orphans:
            by_plan[effective_plan_id(inv.plan_id, inv.issue_date, plans)].append(inv)

    reports = [analyze_plan(p, plans, by_plan[pid], as_of) for pid, p in plans.items()]
    for r in reports:
        plan = plans[r["plan"]["plan_id"]]
        for f in r["findings"]:
            _apply_remediation(f, plan, memos, make_goods)
            _suggest(f, plan)
    orphan_findings = detect_orphans(orphans, plans, as_of)
    _link_orphans(orphan_findings, [f for r in reports for f in r["findings"]],
                  {i.invoice_id: i for i in invoices})

    if plan_id != "all":
        reports = [r for r in reports if r["plan"]["plan_id"] == plan_id]
        orphan_findings = [o for o in orphan_findings
                           if any(c["plan_id"] == plan_id for c in o.alternatives[0]["candidate_plans"])]

    all_findings = [f for r in reports for f in r["findings"]] + orphan_findings
    open_ = [f for f in all_findings if f.status in ("open", "partially_remediated")]
    return {
        "as_of": str(as_of),
        "plans": [{**r, "findings": [f.model_dump(mode="json") for f in r["findings"]]} for r in reports],
        "orphan_invoices": [o.model_dump(mode="json") for o in orphan_findings],
        "summary": {
            "open_findings": len(open_),
            "needs_review": len([f for f in all_findings if f.status == "needs_review"]),
            "remediated": len([f for f in all_findings if f.status == "remediated"]),
            "net_underbilled": round(sum(-f.remaining_delta for f in open_ if f.remaining_delta < 0), 2),
            "net_overbilled": round(sum(f.remaining_delta for f in open_ if f.remaining_delta > 0), 2),
        },
    }


def find_finding(finding_id: str, as_of: date | None = None) -> Finding | None:
    result = investigate("all", as_of)
    for f in [f for r in result["plans"] for f in r["findings"]] + result["orphan_invoices"]:
        if f["finding_id"] == finding_id:
            return Finding(**f)
    return None
