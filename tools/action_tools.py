"""Write tools. propose_* only create drafts. apply/rollback pause the graph with interrupt()
and continue only with the human's decision from the UI: approval is enforced by code,
not by the prompt."""
from typing import Any, Optional

from langchain_core.tools import tool
from langgraph.types import interrupt

from core import actions, repository
from core.actions import ActionError


def _draft(fn, *args) -> dict:
    try:
        p = fn(*args)
    except ActionError as e:
        return {"error": str(e)}
    return {"proposal": p.model_dump(mode="json"),
            "next_step": f"Show the proposal to the user. Only if they ask to apply it, call apply('{p.proposal_id}')."}


@tool
def propose_make_good_invoice(plan_id: str, amount: float, reason: str, finding_id: Optional[str] = None) -> dict:
    """Draft a make-good invoice (recovers missed or underbilled revenue) in the plan currency.
    Pass the finding_id from investigate so the amount is validated against the open gap.
    Creates a pending proposal only; nothing is written to the ledgers."""
    return _draft(actions.build_make_good, plan_id, amount, reason, finding_id)


@tool
def propose_credit_memo(invoice_id: str, amount: float, reason: str, finding_id: Optional[str] = None) -> dict:
    """Draft a credit memo (negative invoice) against an overbilled invoice, in the plan currency.
    Pass the finding_id from investigate. Rejected if the overbilling is already covered (e.g. by an
    existing memo). Creates a pending proposal only."""
    return _draft(actions.build_credit_memo, invoice_id, amount, reason, finding_id)


@tool
def propose_plan_amendment(plan_id: str, change_set: dict[str, Any], reason: str,
                           finding_id: Optional[str] = None) -> dict:
    """Draft a change to the contract itself. change_set may contain total_value, cadence
    (Monthly|Quarterly|Annual), entitlements (full new list) and notes. Use only when the agreement
    changed, after confirming intent with the user. Creates a pending proposal only."""
    return _draft(actions.build_amendment, plan_id, change_set, reason, finding_id)


@tool
def list_proposals(status: Optional[str] = None) -> dict:
    """List proposals (optionally by status: pending, approved, rejected, rolled_back) and the
    applied/rolled-back actions with their action_id and record_id."""
    return {"proposals": actions.list_proposals(status), "actions": repository.read_sandbox("actions.json")}


@tool
def apply(action_draft: str) -> dict:
    """Apply a pending proposal to the sandbox. action_draft is the proposal_id (e.g. "P-001").
    Execution pauses until the human clicks Approve or Reject in the chat UI.
    Call it only when the user asks to apply a proposal."""
    try:
        proposal = actions.get_proposal(action_draft)
    except ActionError as e:
        return {"error": str(e)}
    if proposal["status"] != "pending":
        return {"error": f"Proposal {action_draft} is {proposal['status']}, not pending"}

    decision = interrupt({"type": "apply", "proposal": proposal})  # graph pauses here

    try:
        if decision == "approve":
            return {"approved_by_human": True, **actions.apply(action_draft)}
        return {"approved_by_human": False, **actions.reject(action_draft, "Rejected in chat UI")}
    except ActionError as e:
        return {"error": str(e)}


@tool
def rollback(action_id: Optional[str] = None) -> dict:
    """Undo an applied action (removes the record it wrote to the sandbox). Without action_id it undoes
    the last applied action. Execution pauses until the human approves in the chat UI."""
    if action_id is None:
        target = actions.last_applied_action()
    else:
        target = next((a for a in repository.read_sandbox("actions.json")
                       if a["action_id"] == action_id and a["status"] == "applied"), None)
    if target is None:
        return {"error": f"No applied action {'to roll back' if action_id is None else action_id}"}

    decision = interrupt({"type": "rollback", "action": target})

    if decision != "approve":
        return {"approved_by_human": False, "status": "rollback cancelled by human"}
    try:
        return {"approved_by_human": True, **actions.rollback(target["action_id"])}
    except ActionError as e:
        return {"error": str(e)}
