from langchain_core.tools import tool

from core.detectors import investigate as _investigate


@tool
def investigate(plan_id: str = "all") -> dict:
    """Run the deterministic leakage analysis for one plan_id or "all" plans, as of the configured date.
    Returns, per plan: term, period-by-period table (expected vs billed) and findings. Each finding has
    finding_id, type, signed delta (actual - expected, plan currency; negative = underbilled), remaining_delta
    after existing fixes, calculation, evidence, assumptions, confidence, status and a suggested_action
    (prefilled propose_* call) plus alternatives, and a reconciliation (the plan's total gap must equal
    the sum of its findings). Also returns other_findings (orphan invoices, data quality), a summary,
    and a `method` block describing exactly how the check works and what it does not handle.
    Already-applied sandbox fixes and existing credit memos are taken into account."""
    try:
        return _investigate(plan_id)
    except KeyError as e:
        return {"error": str(e)}
