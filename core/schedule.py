"""What *should* have been billed: plan term and expected periods.

Assumptions (stated in findings):
- A term runs to `end_date` when the plan has one, otherwise 12 months from start_date.
- A plan that is amended by another (`amends`) ends the day before the amendment starts.
- total_value is spread evenly over the contracted term; the last period absorbs rounding.
"""
from calendar import monthrange
from datetime import date, timedelta

from core.models import PERIODS_PER_YEAR, Plan

TERM_MONTHS = 12


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def months_covered(start: date, end: date) -> int:
    """Whole calendar months touched by start..end (inclusive)."""
    return (end.year - start.year) * 12 + end.month - start.month + 1


def contracted_end(plan: Plan) -> date:
    return plan.end_date or add_months(plan.start_date, TERM_MONTHS) - timedelta(days=1)


def superseded_by(plan: Plan, plans: dict[str, Plan]) -> Plan | None:
    children = [p for p in plans.values() if p.amends == plan.plan_id]
    return min(children, key=lambda p: p.start_date) if children else None


def term(plan: Plan, plans: dict[str, Plan]) -> dict:
    natural_end = contracted_end(plan)
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


def contracted_schedule(plan: Plan) -> list[dict]:
    """Billing periods of the full contracted term (ignoring supersession), with amounts that
    add up exactly to total_value."""
    length = 12 // PERIODS_PER_YEAR[plan.cadence]
    end = contracted_end(plan)
    periods, i = [], 0
    while (start := add_months(plan.start_date, i * length)) <= end:
        natural_end = add_months(plan.start_date, (i + 1) * length) - timedelta(days=1)
        p_end = min(natural_end, end)
        periods.append({"start": start, "end": p_end, "months": months_covered(start, p_end)})
        i += 1
    total_months = sum(p["months"] for p in periods)
    for p in periods:
        p["expected"] = round(plan.total_value * p["months"] / total_months, 2)
    # rounding remainder goes to the last period, e.g. 10000/12 -> 11 x 833.33 + 833.37
    periods[-1]["expected"] = round(plan.total_value - sum(p["expected"] for p in periods[:-1]), 2)
    return periods


def per_period_amount(plan: Plan) -> float:
    return contracted_schedule(plan)[0]["expected"]


def expected_periods(plan: Plan, plans: dict[str, Plan], as_of: date) -> list[dict]:
    """Billing periods inside the effective term. `due` = period started on or before as_of.
    `partial` = cut short by an amendment (a stub); its amount is prorated by months."""
    t = term(plan, plans)
    periods = []
    for p in contracted_schedule(plan):
        if p["start"] > t["end"]:
            break
        partial = p["end"] > t["end"]
        end = t["end"] if partial else p["end"]
        expected = round(p["expected"] * months_covered(p["start"], end) / p["months"], 2) if partial \
            else p["expected"]
        periods.append({"start": p["start"], "end": end, "expected": expected,
                        "partial": partial, "due": p["start"] <= as_of})
    return periods
