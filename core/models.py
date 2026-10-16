"""Typed records. Input data (Plan, Invoice, CreditMemo) and what the agent produces
(Finding, Proposal, Action)."""
from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Cadence = Literal["Monthly", "Quarterly", "Annual"]
PERIODS_PER_YEAR = {"Monthly": 12, "Quarterly": 4, "Annual": 1}


class Plan(BaseModel):
    plan_id: str
    customer_name: str
    total_value: float
    currency: str
    cadence: Cadence
    start_date: date
    entitlements: list[str] = []
    notes: str = ""
    amends: Optional[str] = None
    end_date: Optional[date] = None  # optional; without it the term is assumed to be 12 months
    amended_by_sandbox: list[str] = []  # AMD-xxx ids applied on top of the original


class Invoice(BaseModel):
    invoice_id: str
    plan_id: str = ""
    customer_name: str
    issue_date: date
    due_date: Optional[date] = None
    amount_invoiced: float
    currency: str
    status: str = ""
    description: str = ""
    # Set only on make-good invoices written by the agent to the sandbox
    kind: Literal["regular", "make_good"] = "regular"
    finding_id: Optional[str] = None
    source: Literal["data", "sandbox"] = "data"


class CreditMemo(BaseModel):
    memo_id: str
    plan_id: str
    invoice_id: str
    amount: float
    currency: str
    issue_date: date
    reason: str = ""
    finding_id: Optional[str] = None
    source: Literal["data", "sandbox"] = "data"


class Money(BaseModel):
    amount: float
    currency: str


class Finding(BaseModel):
    finding_id: str
    type: Literal[
        "missing_period", "underbilled", "overbilled", "currency_mismatch",
        "duplicate_invoice", "out_of_term", "orphan_invoice", "entitlement_gap",
        "plan_inconsistency", "fx_rate_missing", "collection_risk", "unmatched_credit",
        "data_quality", "unexplained_difference",
    ]
    plan_id: Optional[str]
    customer_name: str = ""
    invoice_ids: list[str] = []
    period: Optional[dict[str, str]] = None  # {"start": ..., "end": ...}
    expected: Optional[Money] = None
    actual: Optional[Money] = None
    delta: float = 0.0  # actual - expected, plan currency. <0 underbilled, >0 overbilled
    remaining_delta: float = 0.0  # delta still open after existing remediations
    calculation: str = ""
    evidence: list[str] = []
    assumptions: list[str] = []
    confidence: Literal["high", "medium", "low"] = "high"
    status: Literal["open", "partially_remediated", "remediated", "needs_review"] = "open"
    existing_remediation: list[str] = []  # ids of memos / make-goods that cover it
    suggested_action: Optional[dict[str, Any]] = None  # {"tool": ..., "args": {...}}
    alternatives: list[dict[str, Any]] = []


class Proposal(BaseModel):
    proposal_id: str
    kind: Literal["make_good_invoice", "credit_memo", "plan_amendment"]
    payload: dict[str, Any]  # the record that will be written on apply (id assigned then)
    reason: str
    finding_id: Optional[str] = None
    warnings: list[str] = []
    status: Literal["pending", "approved", "rejected", "rolled_back"] = "pending"
    created_at: str


class Action(BaseModel):
    action_id: str
    proposal_id: str
    kind: str
    ledger: str  # sandbox file the record was written to
    record_id: str
    status: Literal["applied", "rolled_back"] = "applied"
    applied_at: str
    rolled_back_at: Optional[str] = None
