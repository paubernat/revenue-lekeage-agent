"""Compare expected billing (schedule.py) with actual invoices and produce Findings.

Deterministic: same data + same as_of => same findings with the same ids. Ambiguity is
never hidden. It goes into `assumptions`, `confidence` and `alternatives` so the agent
can ask the human. Cross-plan checks (orphans, credits, data quality, safety net) live in checks.py.
"""
import re
from collections import defaultdict
from datetime import date

import config
from core import checks, repository
from core.fx import FxError, fx_convert
from core.models import PERIODS_PER_YEAR, CreditMemo, Finding, Invoice, Money, Plan
from core.schedule import effective_plan_id, expected_periods, per_period_amount, term

EPS = 0.01
VOID_STATUSES = {"void", "voided", "cancelled", "canceled"}

# Returned with every investigation so the agent explains its method from facts, not guesses.
METHOD = {
    "term": "Runs to end_date if the plan has one, else 12 months from start_date; ends the day before an "
            "amending plan (amends) starts. The cut-off stub is offered as an alternative, not as a gap.",
    "expected": "total_value spread evenly over the contracted term by cadence; last period absorbs rounding. "
                "A period is due if it started on or before the as-of date.",
    "matching": "Invoices are matched to plans by plan_id, following the amendment chain by issue date. Each "
                "invoice goes to the period containing its issue_date. If that period is already billed: to the "
                f"earliest unbilled earlier period (late billing), or to the next period if issued at most "
                f"{config.EARLY_BILLING_DAYS} days before it (early billing). Only invoices worth at least "
                f"{config.REASSIGN_MIN_SHARE:.0%} of the period are moved. Otherwise they share the period: "
                "amounts are summed (split billing, refunds); a second full-amount invoice is a duplicate.",
    "currency": "Converted to the plan currency with the rate on the issue date or the closest earlier one. "
                "No rate -> fx_rate_missing, the period is not judged.",
    "excluded": "Void/cancelled invoices are not counted. Invalid records are ignored and reported as data_quality.",
    "remediation": "Existing credit memos and applied sandbox fixes are netted against findings "
                   "(remaining_delta, status remediated). Credit memos no overbilling explains -> unmatched_credit.",
    "collection": "Unpaid invoices past their due date -> collection_risk (billed but not collected).",
    "safety_net": "Per plan, billed_to_date - expected_to_date must equal the sum of finding deltas; any "
                  "leftover -> unexplained_difference.",
    "not_handled": ["A late invoice in a period nobody else billed is labelled with that period (amount right, "
                    "month may be off)", "Pauses, usage-based billing, taxes, price changes without an "
                    "amending plan", "Orphan invoices are never attributed automatically"],
}


def _to_plan_ccy(amount: float, ccy: str, plan: Plan, on: date) -> dict:
    try:
        return fx_convert(amount, ccy, plan.currency, on)
    except FxError as e:
        return {"amount": None, "error": str(e)}


def _period_label(p: dict) -> dict:
    return {"start": str(p["start"]), "end": str(p["end"])}


def _contains(p: dict, d: date) -> bool:
    return p["start"] <= d <= p["end"]


def _assign(inv: Invoice, amount: float, home: dict, periods: list[dict]) -> tuple[dict, str | None]:
    """Pick the billing period an invoice pays for. Returns (period, tag)."""
    if not home["invoices"] and not home["partial"]:
        return home, None
    if amount > 0:
        earlier = [p for p in periods if p["due"] and not p["partial"] and not p["invoices"]
                   and p["start"] < home["start"]]
        if earlier and amount >= config.REASSIGN_MIN_SHARE * earlier[0]["expected"]:
            return earlier[0], "late"
        later = [p for p in periods if p["start"] > home["start"]]
        nxt = later[0] if later else None
        if (nxt and not nxt["invoices"] and not nxt["partial"]
                and 0 < (nxt["start"] - inv.issue_date).days <= config.EARLY_BILLING_DAYS
                and amount >= config.REASSIGN_MIN_SHARE * nxt["expected"]):
            return nxt, "early"
    return home, None


# ---------------------------------------------------------------- per plan

def analyze_plan(plan: Plan, plans: dict[str, Plan], invoices: list[Invoice], as_of: date) -> dict:
    t = term(plan, plans)
    periods = expected_periods(plan, plans, as_of)
    for p in periods:
        p.update(invoices=[], duplicates=[], voids=[], unverified=False)  # invoices: (inv, conv, tag)
    findings: list[Finding] = []
    base = dict(plan_id=plan.plan_id, customer_name=plan.customer_name)
    term_note = (f"Term {t['start']}..{t['end']} ("
                 + ("end_date of the plan" if plan.end_date else "12 months from start_date")
                 + (f", cut short because {t['superseded_by']} supersedes it" if t["superseded_by"] else "") + ")")

    # 1. Assign each invoice to a billing period
    for inv in sorted(invoices, key=lambda i: (i.issue_date, i.invoice_id)):
        home = next((p for p in periods if _contains(p, inv.issue_date)), None)
        if inv.status.lower() in VOID_STATUSES:
            if home:
                home["voids"].append(inv)
            continue
        if home is None:
            findings.append(Finding(
                **base, finding_id=f"F-{plan.plan_id}-out_of_term-{inv.invoice_id}", type="out_of_term",
                invoice_ids=[inv.invoice_id], actual=Money(amount=inv.amount_invoiced, currency=inv.currency),
                calculation=f"{inv.invoice_id} issued {inv.issue_date}, outside term {t['start']}..{t['end']}",
                evidence=[inv.invoice_id], assumptions=[term_note], confidence="low", status="needs_review",
            ))
            continue
        conv = _to_plan_ccy(inv.amount_invoiced, inv.currency, plan, inv.issue_date)
        if conv["amount"] is None:
            home["unverified"] = True
            home["invoices"].append((inv, conv, "no_fx"))
            findings.append(Finding(
                **base, finding_id=f"F-{plan.plan_id}-fx_rate_missing-{inv.invoice_id}", type="fx_rate_missing",
                invoice_ids=[inv.invoice_id], period=_period_label(home),
                actual=Money(amount=inv.amount_invoiced, currency=inv.currency),
                calculation=f"{inv.invoice_id} billed {inv.amount_invoiced:g} {inv.currency}: {conv['error']}. "
                            f"Period {home['start']}..{home['end']} cannot be verified",
                evidence=[inv.invoice_id], confidence="low", status="needs_review",
                assumptions=["Add the missing exchange rate to verify this period"],
            ))
            continue
        target, tag = _assign(inv, conv["amount"], home, periods)
        target["invoices"].append((inv, conv, tag))

    # 2. Compare each period's expected amount with what was billed
    for p in periods:
        if p["unverified"]:
            continue
        label = _period_label(p)
        key = f"{plan.plan_id}-{{}}-{p['start']}"
        void_notes = [f"{v.invoice_id} ({v.amount_invoiced:g} {v.currency}, issued {v.issue_date}) is "
                      f"'{v.status}' and not counted as billed" for v in p["voids"]]
        if not p["invoices"]:
            if p["due"] and not p["partial"]:
                findings.append(Finding(
                    **base, finding_id="F-" + key.format("missing_period"), type="missing_period",
                    period=label, expected=Money(amount=p["expected"], currency=plan.currency),
                    actual=Money(amount=0, currency=plan.currency), delta=-p["expected"],
                    calculation=f"{plan.total_value:g} {plan.currency} over the term, {plan.cadence.lower()} -> "
                                f"{p['expected']:g} expected; no invoice for {p['start']}..{p['end']} "
                                f"-> gap {p['expected']:g}",
                    evidence=[f"No {plan.plan_id} invoice dated {p['start']}..{p['end']}"] +
                             [v.invoice_id for v in p["voids"]],
                    assumptions=[term_note, f"Period counted as due because it started on or before as-of {as_of}"]
                                + void_notes,
                ))
            continue

        # A second invoice for the full period amount is a duplicate, not a split payment
        full = [x for x in p["invoices"] if abs(x[1]["amount"] - p["expected"]) < EPS]
        for dup in full[1:]:
            p["invoices"].remove(dup)
            p["duplicates"].append(dup)
            inv, conv, _ = dup
            findings.append(Finding(
                **base, finding_id=f"F-{plan.plan_id}-duplicate_invoice-{inv.invoice_id}",
                type="duplicate_invoice", invoice_ids=[inv.invoice_id, full[0][0].invoice_id],
                period=label, expected=Money(amount=0, currency=plan.currency),
                actual=Money(amount=conv["amount"], currency=plan.currency), delta=conv["amount"],
                calculation=f"Period {p['start']}..{p['end']} already billed in full by {full[0][0].invoice_id}; "
                            f"{inv.invoice_id} adds {conv['amount']:g} {plan.currency}",
                evidence=[inv.invoice_id, full[0][0].invoice_id], confidence="medium",
                assumptions=["One full invoice per billing period"],
            ))

        items = p["invoices"]
        total = round(sum(c["amount"] for _, c, _ in items), 2)
        delta = round(total - p["expected"], 2)
        if abs(delta) < EPS:
            continue
        ccy_differs = any(i.currency != plan.currency for i, _, _ in items)
        ftype = "currency_mismatch" if ccy_differs else ("underbilled" if delta < 0 else "overbilled")
        parts = [f"{i.invoice_id} {i.amount_invoiced:g} {i.currency}"
                 + (f" ({c['calculation']})" if i.currency != plan.currency else "") for i, c, _ in items]
        calc = (f"Expected {p['expected']:g} {plan.currency}; billed {' + '.join(parts)}"
                + (f" = {total:g} {plan.currency}" if len(items) > 1 or ccy_differs else "")
                + f"; delta {delta:+g} {plan.currency}")
        assumptions, confidence = [term_note], "high"
        for i, c, tag in items:
            if i.currency != plan.currency:
                assumptions.append(f"{i.invoice_id} in {i.currency} but plan in {plan.currency}; converted at "
                                   f"{c['rate']} ({c['rate_date']})")
                if c.get("note"):
                    assumptions.append(c["note"])
            if tag:
                confidence = "medium"
                assumptions.append(f"{i.invoice_id} (issued {i.issue_date}) assumed to be {tag} billing for "
                                   f"{p['start']}..{p['end']}")
        if len(items) > 1:
            assumptions.append(f"Period billed in {len(items)} invoices; amounts summed")
        refunds = [i for i, _, _ in items if i.amount_invoiced < 0]
        if refunds:
            confidence = "low"
            assumptions.append(f"Includes negative invoice(s) {[i.invoice_id for i in refunds]} "
                               f"({'; '.join(i.description for i in refunds)}): if it was an agreed refund "
                               "or SLA credit, there is no leakage. Confirm before recovering it")
        findings.append(Finding(
            **base, finding_id="F-" + key.format(ftype), type=ftype, invoice_ids=[i.invoice_id for i, _, _ in items],
            period=label, expected=Money(amount=p["expected"], currency=plan.currency),
            actual=Money(amount=total, currency=plan.currency), delta=delta, calculation=calc,
            evidence=[i.invoice_id for i, _, _ in items], assumptions=assumptions + void_notes,
            confidence=confidence,
        ))

    # 3. Partial stub at the end of a superseded term: prorated billing is an alternative reading
    for p in periods:
        if p["partial"] and p["due"] and not p["invoices"] and not p["unverified"]:
            stub = (f"Stub {p['start']}..{p['end']} before {t['superseded_by']} took over: if billed prorated, "
                    f"{p['expected']:g} {plan.currency} more is due")
            prior = [f for f in findings if f.delta < 0]
            if prior:
                f = prior[-1]
                f.confidence = "medium" if f.confidence == "high" else f.confidence
                f.assumptions.append(f"Stub period {p['start']}..{p['end']} treated as not billable (superseded)")
                f.alternatives.append({
                    "description": stub, "delta": round(f.delta - p["expected"], 2),
                    "calculation": f"{f.delta:g} - {p['expected']:g} = {round(f.delta - p['expected'], 2):g}",
                })
            else:
                findings.append(Finding(
                    **base, finding_id=f"F-{plan.plan_id}-stub-{p['start']}", type="missing_period",
                    period=_period_label(p), calculation=stub, confidence="low", status="needs_review",
                    assumptions=[term_note, "Prorated by whole months; billable only if the contract says so"],
                ))

    findings += _entitlement_gaps(plan, invoices, findings)
    findings += _plan_inconsistency(plan)

    # 4. Safety net: every dollar due and every dollar billed must be explained by the findings
    judged = [p for p in periods if not p["unverified"]
              and ((p["due"] and not p["partial"]) or p["invoices"] or p["duplicates"])]
    expected_to_date = sum(p["expected"] for p in judged)
    billed_to_date = sum(c["amount"] for p in judged for _, c, _ in p["invoices"] + p["duplicates"])
    reconciliation, leftover = checks.reconcile(plan, expected_to_date, billed_to_date, findings)
    if leftover:
        findings.append(leftover)

    return {
        "plan": plan.model_dump(mode="json"),
        "term": {k: str(v) if v else None for k, v in t.items()},
        "per_period_amount": per_period_amount(plan),
        "periods": [_period_row(p, plan) for p in periods],
        "reconciliation": reconciliation,
        "findings": findings,
    }


def _period_row(p: dict, plan: Plan) -> dict:
    counted = p["invoices"] + p["duplicates"]
    billed = round(sum(c["amount"] or 0 for _, c, _ in counted if c.get("amount") is not None), 2)
    if p["unverified"]:
        status = "unverified_missing_fx_rate"
    elif not counted:
        status = "not_due_yet" if not p["due"] else ("partial_stub_unbilled" if p["partial"] else "missing")
    elif abs(billed - p["expected"]) < EPS:
        status = "ok"
    else:
        status = "underbilled" if billed < p["expected"] else "overbilled"
    return {
        **_period_label(p), "expected": p["expected"], "currency": plan.currency, "billed_in_plan_ccy": billed,
        "invoices": [f"{i.invoice_id} ({i.amount_invoiced:g} {i.currency}, {i.issue_date}{', ' + tag if tag else ''})"
                     for i, _, tag in p["invoices"]]
                    + [f"{i.invoice_id} (duplicate)" for i, _, _ in p["duplicates"]]
                    + [f"{v.invoice_id} ({v.status}, not counted)" for v in p["voids"]],
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
                f.confidence = "medium" if f.confidence == "high" else f.confidence
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
    """Amounts written in the notes (a number next to a currency, e.g. '~8000 USD') that
    contradict total_value / cadence."""
    text = plan.notes.replace(",", "")
    amount = r"(\d+(?:\.\d+)?)"
    ccy = r"(?:USD|EUR|GBP|\$|€|£)"
    numbers = [float(a or b) for a, b in re.findall(rf"{amount}\s*{ccy}|{ccy}\s*{amount}", text)]
    expected = per_period_amount(plan)
    bad = [n for n in numbers if abs(n - expected) > EPS and abs(n - plan.total_value) > EPS]
    if not bad:
        return []
    return [Finding(
        plan_id=plan.plan_id, customer_name=plan.customer_name,
        finding_id=f"F-{plan.plan_id}-plan_inconsistency", type="plan_inconsistency",
        calculation=f"Notes mention {bad}; plan says {plan.total_value:g} {plan.cadence.lower()} = "
                    f"{expected:g} per period",
        evidence=[f"Plan notes: '{plan.notes}'"], confidence="low", status="needs_review",
        assumptions=["Either the notes or the plan fields are wrong; confirm which before acting"],
    )]


# ---------------------------------------------------------------- remediation

def _apply_remediation(f: Finding, plan: Plan, memos: list[CreditMemo], make_goods: list[Invoice]) -> set[str]:
    """Net existing fixes (data credit memos like M-300, or sandbox actions) against the finding.
    Returns the ids of the memos used, so unexplained credits can be detected."""
    covered, ids = 0.0, []
    if f.delta > 0:
        sources = [(m.memo_id, m.amount, m.currency, m.issue_date) for m in memos
                   if m.finding_id == f.finding_id or (m.finding_id is None and m.invoice_id in f.invoice_ids)]
    elif f.delta < 0:
        sources = [(i.invoice_id, i.amount_invoiced, i.currency, i.issue_date) for i in make_goods
                   if i.finding_id == f.finding_id]
    else:
        sources = []
    for rid, amount, ccy, on in sources:
        try:
            covered += fx_convert(amount, ccy, plan.currency, on)["amount"]
            ids.append(rid)
        except FxError as e:
            f.assumptions.append(f"{rid} could not be netted: {e}")
    f.existing_remediation = ids
    sign = 1 if f.delta > 0 else -1
    f.remaining_delta = round(sign * max(abs(f.delta) - covered, 0), 2) + 0.0  # + 0.0 avoids -0.0
    if ids:
        f.status = "remediated" if abs(f.remaining_delta) < EPS else "partially_remediated"
        f.calculation += f"; already covered {covered:g} by {ids} -> remaining {f.remaining_delta:+g}"
    return set(ids) if f.delta > 0 else set()


def _suggest(f: Finding, plan: Plan) -> None:
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

    reports = {pid: analyze_plan(p, plans, by_plan[pid], as_of) for pid, p in plans.items()}
    used_memos: set[str] = set()
    for pid, r in reports.items():
        for f in r["findings"]:
            used_memos |= _apply_remediation(f, plans[pid], memos, make_goods)
            _suggest(f, plans[pid])
        r["findings"] += checks.collection_risk(plans[pid], by_plan[pid], as_of)

    by_id = {i.invoice_id: i for i in invoices}
    other: list[Finding] = []
    for f in checks.unmatched_credits(memos, used_memos, plans, by_id):
        (reports[f.plan_id]["findings"] if f.plan_id in reports else other).append(f)
    orphan_findings = checks.detect_orphans(orphans, plans)
    checks.link_orphans(orphan_findings, [f for r in reports.values() for f in r["findings"]], by_id)
    other += orphan_findings + checks.data_quality(repository.load_issues())

    selected = list(reports.values())
    if plan_id != "all":
        selected = [reports[plan_id]]
        other = [o for o in orphan_findings
                 if any(c["plan_id"] == plan_id for c in o.alternatives[0]["candidate_plans"])]

    all_findings = [f for r in selected for f in r["findings"]] + other
    open_ = [f for f in all_findings if f.status in ("open", "partially_remediated")]
    by_type: dict[str, int] = defaultdict(int)
    for f in all_findings:
        by_type[f.type] += 1
    return {
        "as_of": str(as_of),
        "method": METHOD,
        "plans": [{**r, "findings": [f.model_dump(mode="json") for f in r["findings"]]} for r in selected],
        "other_findings": [o.model_dump(mode="json") for o in other],
        "summary": {
            "open_findings": len(open_),
            "needs_review": len([f for f in all_findings if f.status == "needs_review"]),
            "remediated": len([f for f in all_findings if f.status == "remediated"]),
            "by_type": dict(by_type),
            "net_underbilled": round(sum(-f.remaining_delta for f in open_ if f.remaining_delta < 0), 2),
            "net_overbilled": round(sum(f.remaining_delta for f in open_ if f.remaining_delta > 0), 2),
            "plans_not_reconciled": [r["plan"]["plan_id"] for r in selected if not r["reconciliation"]["ok"]],
        },
    }


def find_finding(finding_id: str, as_of: date | None = None) -> Finding | None:
    result = investigate("all", as_of)
    for f in [f for r in result["plans"] for f in r["findings"]] + result["other_findings"]:
        if f["finding_id"] == finding_id:
            return Finding(**f)
    return None
