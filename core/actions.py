"""Turning findings into fixes, with a human in the loop.

Lifecycle of a fix:

    build_make_good / build_credit_memo / build_amendment
        │   guard rails: _validate_fix / _validate_amendment against the LIVE data
        ▼
    Proposal  status=pending     -> sandbox/proposals.json   (nothing else is written)
        │                  │
     apply()            reject()
        │                  ▼
        │               rejected
        ▼
    record written to its ledger -> INV-MG-001 / CM-001 / AMD-001
    Action   status=applied      -> sandbox/actions.json
    Proposal status=approved     -> + an audit_log.json entry
        │
     rollback()
        ▼
    record removed, Action and Proposal -> rolled_back

apply() and rollback() only run after a human clicks Approve: tools/action_tools.py calls them
behind langgraph's interrupt(). Every step is written to sandbox/audit_log.json.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from core import repository
from core.fx import fx_convert
from core.investigation import find_finding
from core.models import EPS, PERIODS_PER_YEAR, Action, Invoice, Plan, Proposal
from core.schedule import effective_plan_id


@dataclass(frozen=True)
class Ledger:
    file: str      # sandbox file the record is written to
    prefix: str    # id prefix, e.g. INV-MG -> INV-MG-001
    id_field: str  # the record's id field


LEDGERS = {
    "make_good_invoice": Ledger("invoices.json", "INV-MG", "invoice_id"),
    "credit_memo": Ledger("credit_memos.json", "CM", "memo_id"),
    "plan_amendment": Ledger("plan_amendments.json", "AMD", "amendment_id"),
}
AMENDABLE = {"total_value", "cadence", "entitlements", "notes"}

# which kind of finding each money fix may close
UNDERBILLED = "underbilled"  # -> make-good invoice
OVERBILLED = "overbilled"    # -> credit memo


class ActionError(ValueError):
    """A fix that must not happen. The message is shown to the agent, which explains it to the user."""


# ================================================================ guard rails

def _validate_fix(finding_id: str | None, plan_id: str, amount: float, direction: str,
                  invoice_id: str | None = None) -> list[str]:
    """Check a money fix against a fresh investigation. Raises ActionError if the fix is wrong;
    returns warnings if it is allowed but deserves attention.

        1. the finding exists                         (ids come from investigate)
        2. it belongs to this plan
        3. it is not already fixed                    (I-9123 is covered by M-300)
        4. it is not review-only                      (orphans, data quality, ...)
        5. the direction matches                      (make-good only for under-, credit only for overbilling)
        6. the invoice is part of the finding         (credit memos)
        7. the amount is at most the open gap         (no 20,000 make-good for an 8,000 gap)
    """
    if not finding_id:
        return ["Not linked to an investigation finding (no finding_id): amount is not verified by the analysis"]
    f = find_finding(finding_id)
    if f is None:                                                                     # 1
        raise ActionError(f"Finding {finding_id} not found in the current investigation")
    if f.plan_id != plan_id:                                                          # 2
        raise ActionError(f"Finding {finding_id} belongs to plan {f.plan_id}, not {plan_id}")
    if f.status == "remediated":                                                      # 3
        raise ActionError(f"Finding {finding_id} is already remediated by {f.existing_remediation}")
    if f.status == "needs_review":                                                    # 4
        raise ActionError(f"Finding {finding_id} needs human review and has no amount to act on")
    if direction == UNDERBILLED and f.remaining_delta >= 0:                           # 5
        raise ActionError(f"Finding {finding_id} is not an underbilling; a make-good does not apply")
    if direction == OVERBILLED and f.remaining_delta <= 0:
        raise ActionError(f"Finding {finding_id} is not an overbilling; a credit memo does not apply")
    if invoice_id and invoice_id not in f.invoice_ids:                                # 6
        raise ActionError(f"Invoice {invoice_id} is not part of finding {finding_id} ({f.invoice_ids})")
    open_gap = abs(f.remaining_delta)
    if amount > open_gap + EPS:                                                       # 7
        raise ActionError(f"Amount {amount:g} exceeds the open gap {open_gap:g} of {finding_id}")

    warnings = []
    if amount < open_gap - EPS:
        warnings.append(f"Partial fix: {amount:g} of {open_gap:g}")
    if f.confidence != "high":
        warnings.append(f"Finding confidence is {f.confidence}: " + "; ".join(f.assumptions[-2:]))
    return warnings


def _billed_and_credited(inv: Invoice, plan: Plan) -> tuple[float, float]:
    """What an invoice billed and what has already been credited on it, both in plan currency.
    All credits on one invoice can never exceed what it billed."""
    billed = fx_convert(inv.amount_invoiced, inv.currency, plan.currency, inv.issue_date)["amount"]
    credited = sum(fx_convert(m.amount, m.currency, plan.currency, m.issue_date)["amount"]
                   for m in repository.get_credit_memos() if m.invoice_id == inv.invoice_id)
    return billed, credited


def _validate_amendment(plan: Plan, change_set: dict[str, Any]) -> dict:
    """Only whitelisted fields, valid values, and an actual change. Returns the previous values."""
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
    return previous


# ================================================================ drafts (proposals)

def build_make_good(plan_id: str, amount: float, reason: str, finding_id: str | None = None) -> Proposal:
    """Draft a new invoice that recovers an underbilling, in the plan currency."""
    plan = repository.get_plans().get(plan_id)
    if plan is None:
        raise ActionError(f"Unknown plan_id {plan_id}")
    if amount <= 0:
        raise ActionError("Amount must be positive")
    warnings = _validate_fix(finding_id, plan_id, amount, UNDERBILLED)
    today = date.today()
    payload = {
        "plan_id": plan_id, "customer_name": plan.customer_name, "issue_date": str(today),
        "due_date": str(today + timedelta(days=30)), "amount_invoiced": round(amount, 2),
        "currency": plan.currency, "status": "unpaid", "description": f"Make-good: {reason}",
        "kind": "make_good", "finding_id": finding_id,
    }
    return _save_proposal("make_good_invoice", payload, reason, finding_id, warnings)


def build_credit_memo(invoice_id: str, amount: float, reason: str, finding_id: str | None = None) -> Proposal:
    """Draft a credit (negative invoice) against an overbilled invoice, in the plan currency."""
    inv = {i.invoice_id: i for i in repository.get_invoices()}.get(invoice_id)
    if inv is None:
        raise ActionError(f"Unknown invoice_id {invoice_id}")
    plans = repository.get_plans()
    plan = plans.get(effective_plan_id(inv.plan_id, inv.issue_date, plans)) if inv.plan_id else None
    if plan is None:
        raise ActionError(f"{invoice_id} is not linked to a plan; attribute it before crediting")
    if amount <= 0:
        raise ActionError("Amount must be positive")
    billed, credited = _billed_and_credited(inv, plan)
    if credited + amount > billed + EPS:
        raise ActionError(f"Credits on {invoice_id} would total {credited + amount:g} {plan.currency}, "
                          f"more than its billed {billed:g}")
    warnings = _validate_fix(finding_id, plan.plan_id, amount, OVERBILLED, invoice_id)
    payload = {
        "plan_id": plan.plan_id, "invoice_id": invoice_id, "amount": round(amount, 2),
        "currency": plan.currency, "issue_date": str(date.today()), "reason": reason, "finding_id": finding_id,
    }
    return _save_proposal("credit_memo", payload, reason, finding_id, warnings)


def build_amendment(plan_id: str, change_set: dict[str, Any], reason: str,
                    finding_id: str | None = None) -> Proposal:
    """Draft a change to the contract itself (total, cadence, entitlements, notes)."""
    plan = repository.get_plans().get(plan_id)
    if plan is None:
        raise ActionError(f"Unknown plan_id {plan_id}")
    previous = _validate_amendment(plan, change_set)
    warnings = ["Plan amendments change the contract itself: confirm it reflects the customer agreement"]
    payload = {"plan_id": plan_id, "change_set": change_set, "previous": previous, "reason": reason,
               "finding_id": finding_id, "effective_date": str(date.today())}
    return _save_proposal("plan_amendment", payload, reason, finding_id, warnings)


def _save_proposal(kind: str, payload: dict, reason: str, finding_id: str | None, warnings: list[str]) -> Proposal:
    proposals = repository.read_sandbox("proposals.json")
    for p in proposals:
        if p["status"] != "pending":
            continue
        if p["kind"] == kind and p["payload"] == payload:  # same draft again -> return it, don't duplicate
            return Proposal(**p)
        if finding_id and p["finding_id"] == finding_id:  # one pending fix per finding
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


# ================================================================ lifecycle: apply / reject / rollback

def apply(proposal_id: str) -> dict:
    """Write an approved proposal to the sandbox (called only after the human clicks Approve)."""
    p = Proposal(**get_proposal(proposal_id))
    if p.status != "pending":
        raise ActionError(f"Proposal {proposal_id} is {p.status}, not pending")
    _revalidate(p)  # the data may have changed since the draft was made

    ledger = LEDGERS[p.kind]
    actions = repository.read_sandbox("actions.json")
    record_id = repository.next_id(ledger.prefix, [a["record_id"] for a in actions])  # ids never reused
    record = {ledger.id_field: record_id, **p.payload, "proposal_id": proposal_id}
    repository.append_sandbox(ledger.file, record)

    action = Action(action_id=repository.next_id("A", [a["action_id"] for a in actions]),
                    proposal_id=proposal_id, kind=p.kind, ledger=ledger.file, record_id=record_id,
                    applied_at=repository.now())
    repository.append_sandbox("actions.json", action.model_dump(mode="json"))
    _set_proposal_status(proposal_id, "approved")
    repository.audit("applied", action_id=action.action_id, proposal_id=proposal_id, kind=p.kind,
                     record_id=record_id, finding_id=p.finding_id, approved_by="human (chat)")
    return {"action": action.model_dump(mode="json"), "record": record}


def _revalidate(p: Proposal) -> None:
    """Run the money guard rails again at apply time, e.g. the gap was closed by another fix meanwhile."""
    if not p.finding_id:
        return
    if p.kind == "make_good_invoice":
        _validate_fix(p.finding_id, p.payload["plan_id"], p.payload["amount_invoiced"], UNDERBILLED)
    elif p.kind == "credit_memo":
        _validate_fix(p.finding_id, p.payload["plan_id"], p.payload["amount"], OVERBILLED, p.payload["invoice_id"])


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
    """Undo an applied action (the last one if no id): remove its record from the ledger."""
    if action_id is None:
        last = last_applied_action()
        if last is None:
            raise ActionError("No applied action to roll back")
        action_id = last["action_id"]
    actions = repository.read_sandbox("actions.json")
    target = next((a for a in actions if a["action_id"] == action_id), None)
    if target is None:
        raise ActionError(f"Unknown action {action_id}")
    if target["status"] != "applied":
        raise ActionError(f"Action {action_id} is already {target['status']}")

    id_field = LEDGERS[target["kind"]].id_field
    records = repository.read_sandbox(target["ledger"])
    repository.write_sandbox(target["ledger"], [r for r in records if r[id_field] != target["record_id"]])
    target["status"], target["rolled_back_at"] = "rolled_back", repository.now()
    repository.write_sandbox("actions.json", actions)
    _set_proposal_status(target["proposal_id"], "rolled_back")
    repository.audit("rolled_back", action_id=action_id, record_id=target["record_id"],
                     proposal_id=target["proposal_id"])
    return {"action_id": action_id, "record_id": target["record_id"], "status": "rolled_back"}
