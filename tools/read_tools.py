"""Read-only tools. They never change anything, so they run without approval."""
from datetime import date
from typing import Optional

from langchain_core.tools import tool

import config
from core import repository
from core.fx import FxError
from core.fx import fx_convert as _fx_convert
from core.schedule import effective_plan_id, expected_periods, per_period_amount, superseded_by, term


@tool
def load_plan(plan_id: str) -> dict:
    """Read a billing plan (contract): customer, total_value, currency, cadence, start_date,
    entitlements, notes, the inferred term, the amendment chain and the expected billing schedule."""
    plans = repository.get_plans()
    plan = plans.get(plan_id)
    if plan is None:
        return {"error": f"Unknown plan_id '{plan_id}'", "known_plan_ids": sorted(plans)}
    t = term(plan, plans)
    child = superseded_by(plan, plans)
    return {
        "plan": plan.model_dump(mode="json"),
        "term": {"start": str(t["start"]), "end": str(t["end"]),
                 "rule": "12 months from start_date, ended early if another plan amends it"},
        "amends": plan.amends,
        "superseded_by": child.plan_id if child else None,
        "per_period_amount": per_period_amount(plan),
        "schedule": [{"start": str(p["start"]), "end": str(p["end"]), "expected": p["expected"],
                      "partial": p["partial"], "due_as_of": p["due"]}
                     for p in expected_periods(plan, plans, config.get_as_of())],
        "as_of": str(config.get_as_of()),
    }


@tool
def query_invoices(plan_id: Optional[str] = None, customer_name: Optional[str] = None,
                   date_from: Optional[str] = None, date_to: Optional[str] = None,
                   include_sandbox: bool = True) -> dict:
    """Filter invoices by plan_id, customer_name (case-insensitive substring) and issue date range
    (ISO dates, inclusive). plan_id also matches invoices that belong to the plan via its amendment
    chain. Use plan_id="" to find orphan invoices with no plan. Sandbox make-good invoices are included
    unless include_sandbox is false."""
    plans = repository.get_plans()
    out = []
    for inv in repository.get_invoices():
        eff = effective_plan_id(inv.plan_id, inv.issue_date, plans) if inv.plan_id else ""
        if plan_id is not None and plan_id not in (inv.plan_id, eff):
            continue
        if customer_name and customer_name.casefold() not in inv.customer_name.casefold():
            continue
        if date_from and inv.issue_date < date.fromisoformat(date_from):
            continue
        if date_to and inv.issue_date > date.fromisoformat(date_to):
            continue
        if not include_sandbox and inv.source == "sandbox":
            continue
        out.append({**inv.model_dump(mode="json"), "effective_plan_id": eff})
    memos = [m.model_dump(mode="json") for m in repository.get_credit_memos()
             if m.invoice_id in {i["invoice_id"] for i in out}]
    return {"count": len(out), "invoices": out, "credit_memos_on_these_invoices": memos}


@tool
def fx_convert(amount: float, from_ccy: str, to_ccy: str, on_date: str) -> dict:
    """Convert an amount between currencies using the rate on on_date (ISO), or the closest earlier
    available rate (reported in `note`)."""
    try:
        return _fx_convert(amount, from_ccy, to_ccy, on_date)
    except FxError as e:
        return {"error": str(e)}
