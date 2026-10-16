"""Drafts (proposals), apply and rollback. Nothing reaches a sandbox ledger until
apply(proposal_id) runs, and the graph only runs it after a human approves."""
from datetime import date, timedelta
from typing import Any

from core import repository
from core.detectors import EPS, find_finding
from core.fx import fx_convert
from core.models import PERIODS_PER_YEAR, Action, Proposal
from core.schedule import effective_plan_id

# kind -> (sandbox ledger, id prefix, id field)
LEDGERS = {
    "make_good_invoice": ("invoices.json", "INV-MG", "invoice_id"),
    "credit_memo": ("credit_memos.json", "CM", "memo_id"),
    "plan_amendment": ("plan_amendments.json", "AMD", "amendment_id"),
}
AMENDABLE = {"total_value", "cadence", "entitlements", "notes"}


class ActionError(ValueError):
    pass


# ---------------------------------------------------------------- validation

def _check_finding(finding_id: str | None, plan_id: str, amount: float, direction: str,
                   invoice_id: str | None = None) -> list[str]:
    """direction: 'underbilled' (make-good) or 'overbilled' (credit memo)."""
    if not finding_id:
        return ["Not linked to an investigation finding (no finding_id): amount is not verified by the analysis"]
    f = find_finding(finding_id)
    if f is None:
        raise ActionError(f"Finding {finding_id} not found in the current investigation")
    if f.plan_id != plan_id:
        raise ActionError(f"Finding {finding_id} belongs to plan {f.plan_id}, not {plan_id}")
    if f.status == "remediated":
        raise ActionError(f"Finding {finding_id} is already remediated by {f.existing_remediation}")
    if f.status == "needs_review":
        raise ActionError(f"Finding {finding_id} needs human review and has no amount to act on")
    if direction == "underbilled" and f.remaining_delta >= 0:
        raise ActionError(f"Finding {finding_id} is not an underbilling; a make-good does not apply")
    if direction == "overbilled" and f.remaining_delta <= 0:
        raise ActionError(f"Finding {finding_id} is not an overbilling; a credit memo does not apply")
    if invoice_id and invoice_id not in f.invoice_ids:
        raise ActionError(f"Invoice {invoice_id} is not part of finding {finding_id} ({f.invoice_ids})")
    if amount > abs(f.remaining_delta) + EPS:
        raise ActionError(f"Amount {amount:g} exceeds the open gap {abs(f.remaining_delta):g} of {finding_id}")
    warnings = []
    if amount < abs(f.remaining_delta) - EPS:
        warnings.append(f"Partial fix: {amount:g} of {abs(f.remaining_delta):g}")
    if f.confidence != "high":
        warnings.append(f"Finding confidence is {f.confidence}: " + "; ".join(f.assumptions[-2:]))
    return warnings


def _save_proposal(kind: str, payload: dict, reason: str, finding_id: str | None, warnings: list[str]) -> Proposal:
    proposals = repository.read_sandbox("proposals.json")
    for p in proposals:  # idempotent: the same pending draft is returned, not duplicated
        if p["status"] == "pending" and p["kind"] == kind and p["payload"] == payload:
            return Proposal(**p)
        if p["status"] == "pending" and finding_id and p["finding_id"] == finding_id:
            raise ActionError(f"Proposal {p['proposal_id']} is already pending for {finding_id}; "
                              "apply or reject it first")
    proposal = Proposal(
        proposal_id=repository.next_id("P", [p["proposal_id"] for p in proposals]),
        kind=kind, payload=payload, reason=reason, finding_id=finding_id, warnings=warnings,
        created_at=repository.now(),
    )
    repository.append_sandbox("proposals.json", proposal.model_dump(mode="json"))
    repository.audit("proposal_created", proposal_id=proposal.proposal_id, kind=kind, finding_id=finding_id,
                     payload=payload)
    return proposal


# ---------------------------------------------------------------- drafts

def build_make_good(plan_id: str, amount: float, reason: str, finding_id: str | None = None) -> Proposal:
    plan = repository.get_plans().get(plan_id)
    if plan is None:
        raise ActionError(f"Unknown plan_id {plan_id}")
    if amount <= 0:
        raise ActionError("Amount must be positive")
    warnings = _check_finding(finding_id, plan_id, amount, "underbilled")
    today = date.today()
    payload = {
        "plan_id": plan_id, "customer_name": plan.customer_name, "issue_date": str(today),
        "due_date": str(today + timedelta(days=30)), "amount_invoiced": round(amount, 2),
        "currency": plan.currency, "status": "unpaid", "description": f"Make-good: {reason}",
        "kind": "make_good", "finding_id": finding_id,
    }
    return _save_proposal("make_good_invoice", payload, reason, finding_id, warnings)


def build_credit_memo(invoice_id: str, amount: float, reason: str, finding_id: str | None = None) -> Proposal:
    invoices = {i.invoice_id: i for i in repository.get_invoices()}
    inv = invoices.get(invoice_id)
    if inv is None:
        raise ActionError(f"Unknown invoice_id {invoice_id}")
    plans = repository.get_plans()
    plan = plans.get(effective_plan_id(inv.plan_id, inv.issue_date, plans)) if inv.plan_id else None
    if plan is None:
        raise ActionError(f"{invoice_id} is not linked to a plan; attribute it before crediting")
    if amount <= 0:
        raise ActionError("Amount must be positive")
    billed = fx_convert(inv.amount_invoiced, inv.currency, plan.currency, inv.issue_date)["amount"]
    credited = sum(fx_convert(m.amount, m.currency, plan.currency, m.issue_date)["amount"]
                   for m in repository.get_credit_memos() if m.invoice_id == invoice_id)
    if credited + amount > billed + EPS:
        raise ActionError(f"Credits on {invoice_id} would total {credited + amount:g} {plan.currency}, "
                          f"more than its billed {billed:g}")
    warnings = _check_finding(finding_id, plan.plan_id, amount, "overbilled", invoice_id)
    payload = {
        "plan_id": plan.plan_id, "invoice_id": invoice_id, "amount": round(amount, 2),
        "currency": plan.currency, "issue_date": str(date.today()), "reason": reason, "finding_id": finding_id,
    }
    return _save_proposal("credit_memo", payload, reason, finding_id, warnings)


def build_amendment(plan_id: str, change_set: dict[str, Any], reason: str,
                    finding_id: str | None = None) -> Proposal:
    plan = repository.get_plans().get(plan_id)
    if plan is None:
        raise ActionError(f"Unknown plan_id {plan_id}")
    unknown = set(change_set) - AMENDABLE
    if unknown or not change_set:
        raise ActionError(f"change_set may only contain {sorted(AMENDABLE)}; got {sorted(change_set)}")
    if "total_value" in change_set and not (isinstance(change_set["total_value"], (int, float))
                                            and change_set["total_value"] > 0):
        raise ActionError("total_value must be a positive number")
    if "cadence" in change_set and change_set["cadence"] not in PERIODS_PER_YEAR:
        raise ActionError(f"cadence must be one of {list(PERIODS_PER_YEAR)}")
    if "entitlements" in change_set and not isinstance(change_set["entitlements"], list):
        raise ActionError("entitlements must be a list of strings")
    previous = {k: getattr(plan, k) for k in change_set}
    if all(previous[k] == v for k, v in change_set.items()):
        raise ActionError("change_set does not change anything")
    warnings = ["Plan amendments change the contract itself: confirm it reflects the customer agreement"]
    payload = {"plan_id": plan_id, "change_set": change_set, "previous": previous, "reason": reason,
               "finding_id": finding_id, "effective_date": str(date.today())}
    return _save_proposal("plan_amendment", payload, reason, finding_id, warnings)


def list_proposals(status: str | None = None) -> list[dict]:
    return [p for p in repository.read_sandbox("proposals.json") if status in (None, p["status"])]


def get_proposal(proposal_id: str) -> dict:
    for p in repository.read_sandbox("proposals.json"):
        if p["proposal_id"] == proposal_id:
            return p
    raise ActionError(f"Unknown proposal {proposal_id}")


def _set_proposal_status(proposal_id: str, status: str) -> None:
    proposals = repository.read_sandbox("proposals.json")
    for p in proposals:
        if p["proposal_id"] == proposal_id:
            p["status"] = status
    repository.write_sandbox("proposals.json", proposals)


# ---------------------------------------------------------------- apply / reject / rollback

def apply(proposal_id: str) -> dict:
    p = Proposal(**get_proposal(proposal_id))
    if p.status != "pending":
        raise ActionError(f"Proposal {proposal_id} is {p.status}, not pending")
    # Re-validate: the world may have changed since the draft was created
    if p.finding_id and p.kind == "make_good_invoice":
        _check_finding(p.finding_id, p.payload["plan_id"], p.payload["amount_invoiced"], "underbilled")
    if p.finding_id and p.kind == "credit_memo":
        _check_finding(p.finding_id, p.payload["plan_id"], p.payload["amount"], "overbilled",
                       p.payload["invoice_id"])

    ledger, prefix, id_field = LEDGERS[p.kind]
    actions = repository.read_sandbox("actions.json")
    used = [a["record_id"] for a in actions]  # ids are never reused, even after rollback
    record_id = repository.next_id(prefix, used)
    record = {id_field: record_id, **p.payload, "proposal_id": proposal_id}
    repository.append_sandbox(ledger, record)

    action = Action(action_id=repository.next_id("A", [a["action_id"] for a in actions]),
                    proposal_id=proposal_id, kind=p.kind, ledger=ledger, record_id=record_id,
                    applied_at=repository.now())
    repository.append_sandbox("actions.json", action.model_dump(mode="json"))
    _set_proposal_status(proposal_id, "approved")
    repository.audit("applied", action_id=action.action_id, proposal_id=proposal_id, kind=p.kind,
                     record_id=record_id, finding_id=p.finding_id, approved_by="human (chat)")
    return {"action": action.model_dump(mode="json"), "record": record}


def reject(proposal_id: str, note: str = "") -> dict:
    p = get_proposal(proposal_id)
    if p["status"] != "pending":
        raise ActionError(f"Proposal {proposal_id} is {p['status']}, not pending")
    _set_proposal_status(proposal_id, "rejected")
    repository.audit("rejected", proposal_id=proposal_id, note=note)
    return {"proposal_id": proposal_id, "status": "rejected"}


def last_applied_action() -> dict | None:
    applied = [a for a in repository.read_sandbox("actions.json") if a["status"] == "applied"]
    return applied[-1] if applied else None


def rollback(action_id: str | None = None) -> dict:
    actions = repository.read_sandbox("actions.json")
    if action_id is None:
        target = last_applied_action()
        if target is None:
            raise ActionError("No applied action to roll back")
        action_id = target["action_id"]
    target = next((a for a in actions if a["action_id"] == action_id), None)
    if target is None:
        raise ActionError(f"Unknown action {action_id}")
    if target["status"] != "applied":
        raise ActionError(f"Action {action_id} is already {target['status']}")

    _, _, id_field = LEDGERS[target["kind"]]
    records = repository.read_sandbox(target["ledger"])
    repository.write_sandbox(target["ledger"], [r for r in records if r[id_field] != target["record_id"]])
    target["status"], target["rolled_back_at"] = "rolled_back", repository.now()
    repository.write_sandbox("actions.json", actions)
    _set_proposal_status(target["proposal_id"], "rolled_back")
    repository.audit("rolled_back", action_id=action_id, record_id=target["record_id"],
                     proposal_id=target["proposal_id"])
    return {"action_id": action_id, "record_id": target["record_id"], "status": "rolled_back"}
