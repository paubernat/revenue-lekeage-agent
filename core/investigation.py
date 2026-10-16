"""The whole investigation, top to bottom. This is what the `investigate` tool runs.

    1. LOAD       plans, invoices, credit memos (data/ + sandbox/), skipping invalid records
    2. ROUTE      each invoice to the plan in effect on its date, or to the orphan pile
    3. DETECT     per plan: detectors.analyze_plan (periods, gaps, safety net)
    4. REMEDIATE  net existing fixes, pre-fill the suggested action  (remediation.py)
    5. CHECK      cross-plan: collection risk, unexplained credits, orphans, data quality  (checks.py)
    6. REPORT     keep the requested plan, add the method note and a summary
"""
from collections import defaultdict
from datetime import date

import config
from core import checks, detectors, remediation, repository
from core.models import Finding, Invoice, Plan
from core.schedule import effective_plan_id

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


def investigate(plan_id: str = "all", as_of: date | None = None) -> dict:
    as_of = as_of or config.get_as_of()

    # 1. load
    plans = repository.get_plans()
    if plan_id != "all" and plan_id not in plans:
        raise KeyError(f"Unknown plan_id '{plan_id}'. Known: {sorted(plans)}")
    invoices = repository.get_invoices()
    memos = repository.get_credit_memos()

    # 2. route
    by_plan, orphans, make_goods = _route_invoices(invoices, plans)

    # 3. detect + 4. remediate
    reports = {pid: detectors.analyze_plan(p, plans, by_plan[pid], as_of) for pid, p in plans.items()}
    used_memos: set[str] = set()
    for pid, report in reports.items():
        for f in report["findings"]:
            used_memos |= remediation.net_existing_fixes(f, plans[pid], memos, make_goods)
            remediation.suggest_action(f, plans[pid])

    # 5. cross-plan checks
    by_id = {i.invoice_id: i for i in invoices}
    unassigned: list[Finding] = []
    for pid, report in reports.items():
        report["findings"] += checks.collection_risk(plans[pid], by_plan[pid], as_of)
    for f in checks.unmatched_credits(memos, used_memos, plans, by_id):
        (reports[f.plan_id]["findings"] if f.plan_id in reports else unassigned).append(f)
    orphan_findings = checks.detect_orphans(orphans, plans)
    checks.link_orphans(orphan_findings, [f for r in reports.values() for f in r["findings"]], by_id)
    unassigned += orphan_findings + checks.data_quality(repository.load_issues())

    # 6. report
    selected = list(reports.values())
    if plan_id != "all":  # one plan: its report + orphans that could belong to it
        selected = [reports[plan_id]]
        unassigned = [o for o in orphan_findings
                      if any(c["plan_id"] == plan_id for c in o.alternatives[0]["candidate_plans"])]
    return {
        "as_of": str(as_of),
        "method": METHOD,
        "plans": [{**r, "findings": [f.model_dump(mode="json") for f in r["findings"]]} for r in selected],
        "other_findings": [o.model_dump(mode="json") for o in unassigned],
        "summary": _summary(selected, unassigned),
    }


def _route_invoices(invoices: list[Invoice], plans: dict[str, Plan]):
    """Regular invoices go to the plan in effect on their issue date (C-1007 -> C-1007-A1 after July).
    Invoices whose plan_id matches no plan are orphans. Make-goods written by the agent are kept apart:
    they are fixes, not billing to be checked."""
    by_plan: dict[str, list[Invoice]] = defaultdict(list)
    orphans, make_goods = [], []
    for inv in invoices:
        if inv.kind == "make_good":
            make_goods.append(inv)
        elif not inv.plan_id or inv.plan_id not in plans:
            orphans.append(inv)
        else:
            by_plan[effective_plan_id(inv.plan_id, inv.issue_date, plans)].append(inv)
    return by_plan, orphans, make_goods


def _summary(reports: list[dict], unassigned: list[Finding]) -> dict:
    findings = [f for r in reports for f in r["findings"]] + unassigned
    open_ = [f for f in findings if f.status in ("open", "partially_remediated")]
    by_type: dict[str, int] = defaultdict(int)
    for f in findings:
        by_type[f.type] += 1
    return {
        "open_findings": len(open_),
        "needs_review": len([f for f in findings if f.status == "needs_review"]),
        "remediated": len([f for f in findings if f.status == "remediated"]),
        "by_type": dict(by_type),
        "net_underbilled": round(sum(-f.remaining_delta for f in open_ if f.remaining_delta < 0), 2),
        "net_overbilled": round(sum(f.remaining_delta for f in open_ if f.remaining_delta > 0), 2),
        "plans_not_reconciled": [r["plan"]["plan_id"] for r in reports if not r["reconciliation"]["ok"]],
    }


def find_finding(finding_id: str, as_of: date | None = None) -> Finding | None:
    """Look a finding up in a fresh investigation (used to validate proposals against live data)."""
    result = investigate("all", as_of)
    for f in [f for r in result["plans"] for f in r["findings"]] + result["other_findings"]:
        if f["finding_id"] == finding_id:
            return Finding(**f)
    return None
