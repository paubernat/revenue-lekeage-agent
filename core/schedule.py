"""What *should* have been billed: plan term and expected periods.

Assumptions (stated in findings):
- A term lasts 12 months from start_date (plans carry no end_date).
- A plan that is amended by another (`amends`) ends the day before the amendment starts.
"""
from calendar import monthrange
from datetime import date, timedelta

from core.models import PERIODS_PER_YEAR, Plan

TERM_MONTHS = 12


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def superseded_by(plan: Plan, plans: dict[str, Plan]) -> Plan | None:
    children = [p for p in plans.values() if p.amends == plan.plan_id]
    return min(children, key=lambda p: p.start_date) if children else None


def term(plan: Plan, plans: dict[str, Plan]) -> dict:
    natural_end = add_months(plan.start_date, TERM_MONTHS) - timedelta(days=1)
    child = superseded_by(plan, plans)
    if child and child.start_date <= natural_end:
        return {"start": plan.start_date, "end": child.start_date - timedelta(days=1),
                "superseded_by": child.plan_id}
    return {"start": plan.start_date, "end": natural_end, "superseded_by": None}


def effective_plan_id(plan_id: str, on_date: date, plans: dict[str, Plan]) -> str:
    """Follow the amendment chain: an invoice dated after an amendment starts belongs to it."""
    current = plans.get(plan_id)
    while current:
        child = superseded_by(current, plans)
        if child and child.start_date <= on_date:
            current = child
        else:
            break
    return current.plan_id if current else plan_id


def per_period_amount(plan: Plan) -> float:
    return round(plan.total_value / PERIODS_PER_YEAR[plan.cadence], 2)


def expected_periods(plan: Plan, plans: dict[str, Plan], as_of: date) -> list[dict]:
    """All billing periods in the term. `due` = period started on or before as_of.
    `partial` = cut short by an amendment; its expected amount is prorated by months."""
    t = term(plan, plans)
    n = PERIODS_PER_YEAR[plan.cadence]
    length = 12 // n
    amount = per_period_amount(plan)
    periods = []
    for i in range(n):
        start = add_months(plan.start_date, i * length)
        if start > t["end"]:
            break
        natural_end = add_months(plan.start_date, (i + 1) * length) - timedelta(days=1)
        partial = natural_end > t["end"]
        end = t["end"] if partial else natural_end
        if partial:
            months = (end.year - start.year) * 12 + end.month - start.month + 1
            expected = round(amount * months / length, 2)
        else:
            expected = amount
        periods.append({"start": start, "end": end, "expected": expected,
                        "partial": partial, "due": start <= as_of})
    return periods
