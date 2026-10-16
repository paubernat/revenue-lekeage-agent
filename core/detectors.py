"""Per-plan analysis: what should have been billed vs. what was billed.

analyze_plan() runs five steps for ONE plan:

    expected periods (schedule.py) ─┐
                                    ├─ 1. ASSIGN  each invoice to the period it pays for
    the plan's invoices ────────────┘            (normal / late / early / shared)
                                       2. JUDGE   each period: missing, under/overbilled,
                                                  currency mismatch, duplicate
                                       3. STUB    if a newer plan cut this one short, offer the
                                                  leftover stub as an alternative reading
                                       4. HINTS   free-text signals: entitlements, notes
                                       5. RECONCILE  safety net: findings must explain the whole gap

Deterministic: same data + same as-of date => same findings, with the same ids.
Ambiguity is never hidden. It goes into `assumptions`, `confidence` and `alternatives`.
"""
import re
from dataclasses import dataclass, field
from datetime import date

import config
from core import checks
from core.fx import FxError, fx_convert
from core.models import EPS, Finding, Invoice, Money, Plan
from core.schedule import expected_periods, per_period_amount, term

VOID_STATUSES = {"void", "voided", "cancelled", "canceled"}


# ---------------------------------------------------------------- the two building blocks

@dataclass
class Billed:
    """One invoice as the plan sees it: its amount converted to the plan currency."""
    invoice: Invoice
    amount: float | None  # in plan currency; None = no FX rate, could not be converted
    fx: dict              # the fx_convert result: rate, rate_date, calculation, note
    tag: str | None = None  # "late" / "early" if moved to another period, "no_fx" if not converted


@dataclass
class Slot:
    """One billing period and everything matched to it."""
    start: date
    end: date
    expected: float
    partial: bool  # a stub cut short by a newer plan
    due: bool      # started on or before the as-of date
    billed: list[Billed] = field(default_factory=list)
    duplicates: list[Billed] = field(default_factory=list)
    voids: list[Invoice] = field(default_factory=list)
    unverified: bool = False  # holds an invoice that could not be converted -> not judged

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    def label(self) -> dict:
        return {"start": str(self.start), "end": str(self.end)}

    @property
    def total(self) -> float:
        return round(sum(b.amount for b in self.billed), 2)

    @property
    def judged(self) -> bool:
        """Counts in the reconciliation: due (and not a stub), or something was billed in it."""
        return not self.unverified and ((self.due and not self.partial) or bool(self.billed or self.duplicates))


def _finding(plan: Plan, **fields) -> Finding:
    return Finding(plan_id=plan.plan_id, customer_name=plan.customer_name, **fields)


def _convert(inv: Invoice, plan: Plan) -> Billed:
    try:
        fx = fx_convert(inv.amount_invoiced, inv.currency, plan.currency, inv.issue_date)
        return Billed(inv, fx["amount"], fx)
    except FxError as e:
        return Billed(inv, None, {"amount": None, "error": str(e)}, tag="no_fx")


# ---------------------------------------------------------------- entry point

def analyze_plan(plan: Plan, plans: dict[str, Plan], invoices: list[Invoice], as_of: date) -> dict:
    t = term(plan, plans)
    slots = [Slot(**p) for p in expected_periods(plan, plans, as_of)]
    term_note = _term_note(plan, t)

    findings = assign_invoices(plan, t, slots, invoices, term_note)            # step 1
    for slot in slots:                                                        # step 2
        findings += judge_period(plan, slot, term_note, as_of)
    add_stub_alternative(plan, t, slots, findings, term_note)                 # step 3
    findings += _entitlement_gaps(plan, invoices, findings)                    # step 4
    findings += _plan_inconsistency(plan)

    judged = [s for s in slots if s.judged]                                   # step 5
    expected_to_date = sum(s.expected for s in judged)
    billed_to_date = sum(b.amount for s in judged for b in s.billed + s.duplicates)
    reconciliation, leftover = checks.reconcile(plan, expected_to_date, billed_to_date, findings)
    if leftover:
        findings.append(leftover)

    return {
        "plan": plan.model_dump(mode="json"),
        "term": {k: str(v) if v else None for k, v in t.items()},
        "per_period_amount": per_period_amount(plan),
        "periods": [_period_row(s, plan) for s in slots],
        "reconciliation": reconciliation,
        "findings": findings,
    }


def _term_note(plan: Plan, t: dict) -> str:
    source = "end_date of the plan" if plan.end_date else "12 months from start_date"
    cut = f", cut short because {t['superseded_by']} supersedes it" if t["superseded_by"] else ""
    return f"Term {t['start']}..{t['end']} ({source}{cut})"


# ---------------------------------------------------------------- step 1: assign

def assign_invoices(plan: Plan, t: dict, slots: list[Slot], invoices: list[Invoice],
                    term_note: str) -> list[Finding]:
    """Put each invoice in the period it pays for. Returns the findings that arise while doing so
    (invoice outside the term, invoice that cannot be converted)."""
    findings = []
    for inv in sorted(invoices, key=lambda i: (i.issue_date, i.invoice_id)):
        home = next((s for s in slots if s.contains(inv.issue_date)), None)

        if inv.status.lower() in VOID_STATUSES:  # cancelled: never counts as billed
            if home:
                home.voids.append(inv)
            continue

        if home is None:  # no period contains the date -> outside the contract
            findings.append(_finding(
                plan, finding_id=f"F-{plan.plan_id}-out_of_term-{inv.invoice_id}", type="out_of_term",
                invoice_ids=[inv.invoice_id], actual=Money(amount=inv.amount_invoiced, currency=inv.currency),
                calculation=f"{inv.invoice_id} issued {inv.issue_date}, outside term {t['start']}..{t['end']}",
                evidence=[inv.invoice_id], assumptions=[term_note], confidence="low", status="needs_review",
            ))
            continue

        billed = _convert(inv, plan)
        if billed.amount is None:  # no FX rate: keep it in its period but don't judge that period
            home.unverified = True
            home.billed.append(billed)
            findings.append(_finding(
                plan, finding_id=f"F-{plan.plan_id}-fx_rate_missing-{inv.invoice_id}", type="fx_rate_missing",
                invoice_ids=[inv.invoice_id], period=home.label(),
                actual=Money(amount=inv.amount_invoiced, currency=inv.currency),
                calculation=f"{inv.invoice_id} billed {inv.amount_invoiced:g} {inv.currency}: {billed.fx['error']}. "
                            f"Period {home.start}..{home.end} cannot be verified",
                evidence=[inv.invoice_id], confidence="low", status="needs_review",
                assumptions=["Add the missing exchange rate to verify this period"],
            ))
            continue

        target, billed.tag = choose_slot(billed, home, slots)
        target.billed.append(billed)
    return findings


def choose_slot(b: Billed, home: Slot, slots: list[Slot]) -> tuple[Slot, str | None]:
    """Which period does this invoice pay for?

    Rule 1, normal: its own period, if nothing was billed there yet.
    Rule 2, late:   own period taken AND an earlier due period is still unbilled -> that one.
                    e.g. C-1007: I-9110 issued 10 Jun pays for Mar-May.
    Rule 3, early:  own period taken AND issued at most EARLY_BILLING_DAYS before the next,
                    still free period -> the next one.  e.g. P-EARLY: 26 Nov pays for December.
    Rule 4, share:  otherwise it shares its own period (split billing, refund or duplicate);
                    judge_period() decides which.
    Rules 2 and 3 only move invoices worth >= REASSIGN_MIN_SHARE of the target period, so a split
    part (400 of 1,000) or a refund (-1,000) stays where it was issued.
    """
    if not home.billed and not home.partial:
        return home, None                                                     # rule 1
    if b.amount > 0:
        def big_enough(s: Slot) -> bool:
            return b.amount >= config.REASSIGN_MIN_SHARE * s.expected

        unbilled_earlier = [s for s in slots if s.due and not s.partial and not s.billed and s.start < home.start]
        if unbilled_earlier and big_enough(unbilled_earlier[0]):
            return unbilled_earlier[0], "late"                                # rule 2

        nxt = next((s for s in slots if s.start > home.start), None)
        days_before = (nxt.start - b.invoice.issue_date).days if nxt else None
        if (nxt and not nxt.billed and not nxt.partial and big_enough(nxt)
                and 0 < days_before <= config.EARLY_BILLING_DAYS):
            return nxt, "early"                                               # rule 3
    return home, None                                                         # rule 4


# ---------------------------------------------------------------- step 2: judge

def judge_period(plan: Plan, slot: Slot, term_note: str, as_of: date) -> list[Finding]:
    """Compare what one period should have billed with what it did bill."""
    if slot.unverified:
        return []  # already reported as fx_rate_missing
    void_notes = [f"{v.invoice_id} ({v.amount_invoiced:g} {v.currency}, issued {v.issue_date}) is "
                  f"'{v.status}' and not counted as billed" for v in slot.voids]

    if not slot.billed:
        if slot.due and not slot.partial:
            return [_missing_period(plan, slot, term_note, as_of, void_notes)]
        return []

    findings = _split_off_duplicates(plan, slot)
    delta = round(slot.total - slot.expected, 2)
    if abs(delta) >= EPS:
        findings.append(_amount_mismatch(plan, slot, delta, term_note, void_notes))
    return findings


def _missing_period(plan: Plan, slot: Slot, term_note: str, as_of: date, void_notes: list[str]) -> Finding:
    return _finding(
        plan, finding_id=f"F-{plan.plan_id}-missing_period-{slot.start}", type="missing_period",
        period=slot.label(), expected=Money(amount=slot.expected, currency=plan.currency),
        actual=Money(amount=0, currency=plan.currency), delta=-slot.expected,
        calculation=f"{plan.total_value:g} {plan.currency} over the term, {plan.cadence.lower()} -> "
                    f"{slot.expected:g} expected; no invoice for {slot.start}..{slot.end} -> gap {slot.expected:g}",
        evidence=[f"No {plan.plan_id} invoice dated {slot.start}..{slot.end}"] + [v.invoice_id for v in slot.voids],
        assumptions=[term_note, f"Period counted as due because it started on or before as-of {as_of}"] + void_notes,
    )


def _split_off_duplicates(plan: Plan, slot: Slot) -> list[Finding]:
    """A second invoice for the FULL period amount is a duplicate, not a split payment.
    It is moved out of slot.billed so it doesn't count twice in the comparison."""
    full = [b for b in slot.billed if abs(b.amount - slot.expected) < EPS]
    findings = []
    for dup in full[1:]:
        slot.billed.remove(dup)
        slot.duplicates.append(dup)
        first = full[0].invoice
        findings.append(_finding(
            plan, finding_id=f"F-{plan.plan_id}-duplicate_invoice-{dup.invoice.invoice_id}",
            type="duplicate_invoice", invoice_ids=[dup.invoice.invoice_id, first.invoice_id],
            period=slot.label(), expected=Money(amount=0, currency=plan.currency),
            actual=Money(amount=dup.amount, currency=plan.currency), delta=dup.amount,
            calculation=f"Period {slot.start}..{slot.end} already billed in full by {first.invoice_id}; "
                        f"{dup.invoice.invoice_id} adds {dup.amount:g} {plan.currency}",
            evidence=[dup.invoice.invoice_id, first.invoice_id], confidence="medium",
            assumptions=["One full invoice per billing period"],
        ))
    return findings


def _amount_mismatch(plan: Plan, slot: Slot, delta: float, term_note: str, void_notes: list[str]) -> Finding:
    """The period was billed, but the (summed, converted) amount differs from the plan."""
    items = slot.billed
    foreign = any(b.invoice.currency != plan.currency for b in items)
    ftype = "currency_mismatch" if foreign else ("underbilled" if delta < 0 else "overbilled")

    # the calculation, e.g. "Expected 25000 USD; billed I-9123 25000 EUR (25000 EUR x 1.08 = 27000 USD) ..."
    parts = [f"{b.invoice.invoice_id} {b.invoice.amount_invoiced:g} {b.invoice.currency}"
             + (f" ({b.fx['calculation']})" if b.invoice.currency != plan.currency else "") for b in items]
    calc = (f"Expected {slot.expected:g} {plan.currency}; billed {' + '.join(parts)}"
            + (f" = {slot.total:g} {plan.currency}" if len(items) > 1 or foreign else "")
            + f"; delta {delta:+g} {plan.currency}")

    # every guess we made, and how sure we are
    assumptions, confidence = [term_note], "high"
    for b in items:
        if b.invoice.currency != plan.currency:
            assumptions.append(f"{b.invoice.invoice_id} in {b.invoice.currency} but plan in {plan.currency}; "
                               f"converted at {b.fx['rate']} ({b.fx['rate_date']})")
            if b.fx.get("note"):
                assumptions.append(b.fx["note"])
        if b.tag:  # moved by choose_slot
            confidence = "medium"
            assumptions.append(f"{b.invoice.invoice_id} (issued {b.invoice.issue_date}) assumed to be {b.tag} "
                               f"billing for {slot.start}..{slot.end}")
    if len(items) > 1:
        assumptions.append(f"Period billed in {len(items)} invoices; amounts summed")
    refunds = [b.invoice for b in items if b.invoice.amount_invoiced < 0]
    if refunds:
        confidence = "low"
        assumptions.append(f"Includes negative invoice(s) {[i.invoice_id for i in refunds]} "
                           f"({'; '.join(i.description for i in refunds)}): if it was an agreed refund "
                           "or SLA credit, there is no leakage. Confirm before recovering it")

    ids = [b.invoice.invoice_id for b in items]
    return _finding(
        plan, finding_id=f"F-{plan.plan_id}-{ftype}-{slot.start}", type=ftype, invoice_ids=ids,
        period=slot.label(), expected=Money(amount=slot.expected, currency=plan.currency),
        actual=Money(amount=slot.total, currency=plan.currency), delta=delta, calculation=calc,
        evidence=ids, assumptions=assumptions + void_notes, confidence=confidence,
    )


# ---------------------------------------------------------------- step 3: stub

def add_stub_alternative(plan: Plan, t: dict, slots: list[Slot], findings: list[Finding], term_note: str) -> None:
    """When a newer plan cuts this one short, the leftover stub (C-1007: June) may or may not be
    billable. Primary reading: not billable. The other reading is attached as an alternative."""
    for slot in slots:
        if not (slot.partial and slot.due and not slot.billed and not slot.unverified):
            continue
        stub = (f"Stub {slot.start}..{slot.end} before {t['superseded_by']} took over: if billed prorated, "
                f"{slot.expected:g} {plan.currency} more is due")
        gaps = [f for f in findings if f.delta < 0]
        if gaps:  # attach to the last gap of this plan
            f = gaps[-1]
            f.confidence = "medium" if f.confidence == "high" else f.confidence
            f.assumptions.append(f"Stub period {slot.start}..{slot.end} treated as not billable (superseded)")
            f.alternatives.append({
                "description": stub, "delta": round(f.delta - slot.expected, 2),
                "calculation": f"{f.delta:g} - {slot.expected:g} = {round(f.delta - slot.expected, 2):g}",
            })
        else:
            findings.append(_finding(
                plan, finding_id=f"F-{plan.plan_id}-stub-{slot.start}", type="missing_period",
                period=slot.label(), calculation=stub, confidence="low", status="needs_review",
                assumptions=[term_note, "Prorated by whole months; billable only if the contract says so"],
            ))


# ---------------------------------------------------------------- step 4: hints from free text

def _entitlement_gaps(plan: Plan, invoices: list[Invoice], findings: list[Finding]) -> list[Finding]:
    """Only when the plan notes ask to watch entitlement coverage (C-1010).
    An invoice that doesn't mention an entitlement may mean the customer bought less, so the
    underbilling on that invoice gets a second option: amend the plan instead of invoicing."""
    if "entitlement" not in plan.notes.lower():
        return []
    out = []
    for inv in invoices:
        missing = [e for e in plan.entitlements if e.lower() not in inv.description.lower()]
        if not missing:
            continue
        out.append(_finding(
            plan, finding_id=f"F-{plan.plan_id}-entitlement_gap-{inv.invoice_id}", type="entitlement_gap",
            invoice_ids=[inv.invoice_id],
            calculation=f"Plan entitlements {plan.entitlements}; {inv.invoice_id} description "
                        f"'{inv.description}' does not mention {missing}",
            evidence=[inv.invoice_id, f"Plan notes: '{plan.notes}'"], confidence="low", status="needs_review",
            assumptions=["Inferred from free-text description; intent must be confirmed by a human"],
        ))
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
    """An amount written in the notes next to a currency ('~8000 USD') that contradicts the fields."""
    text = plan.notes.replace(",", "")
    amount = r"(\d+(?:\.\d+)?)"
    ccy = r"(?:USD|EUR|GBP|\$|€|£)"
    numbers = [float(a or b) for a, b in re.findall(rf"{amount}\s*{ccy}|{ccy}\s*{amount}", text)]
    expected = per_period_amount(plan)
    bad = [n for n in numbers if abs(n - expected) > EPS and abs(n - plan.total_value) > EPS]
    if not bad:
        return []
    return [_finding(
        plan, finding_id=f"F-{plan.plan_id}-plan_inconsistency", type="plan_inconsistency",
        calculation=f"Notes mention {bad}; plan says {plan.total_value:g} {plan.cadence.lower()} = "
                    f"{expected:g} per period",
        evidence=[f"Plan notes: '{plan.notes}'"], confidence="low", status="needs_review",
        assumptions=["Either the notes or the plan fields are wrong; confirm which before acting"],
    )]


# ---------------------------------------------------------------- output table

def _period_row(slot: Slot, plan: Plan) -> dict:
    """One line of the period-by-period table the agent shows ("what about the other months?")."""
    counted = slot.billed + slot.duplicates
    billed = round(sum(b.amount for b in counted if b.amount is not None), 2)
    if slot.unverified:
        status = "unverified_missing_fx_rate"
    elif not counted:
        status = "not_due_yet" if not slot.due else ("partial_stub_unbilled" if slot.partial else "missing")
    elif abs(billed - slot.expected) < EPS:
        status = "ok"
    else:
        status = "underbilled" if billed < slot.expected else "overbilled"
    return {
        **slot.label(), "expected": slot.expected, "currency": plan.currency, "billed_in_plan_ccy": billed,
        "invoices": [f"{b.invoice.invoice_id} ({b.invoice.amount_invoiced:g} {b.invoice.currency}, "
                     f"{b.invoice.issue_date}{', ' + b.tag if b.tag else ''})" for b in slot.billed]
                    + [f"{b.invoice.invoice_id} (duplicate)" for b in slot.duplicates]
                    + [f"{v.invoice_id} ({v.status}, not counted)" for v in slot.voids],
        "partial": slot.partial, "status": status,
    }
