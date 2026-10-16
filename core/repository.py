"""The only module that touches disk. Reads data/ (read-only) merged with sandbox/
(writable), so applied actions are visible to the next investigation."""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from core.models import CreditMemo, Invoice, Plan

SANDBOX_FILES = [
    "invoices.json", "credit_memos.json", "plan_amendments.json",
    "proposals.json", "actions.json", "audit_log.json",
]


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else []


def read_sandbox(name: str) -> list[dict]:
    return _read(config.SANDBOX_DIR / name)


def write_sandbox(name: str, records: list[dict]) -> None:
    config.SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    (config.SANDBOX_DIR / name).write_text(
        json.dumps(records, indent=2, default=str), encoding="utf-8"
    )


def append_sandbox(name: str, record: dict) -> None:
    write_sandbox(name, read_sandbox(name) + [record])


def reset_sandbox() -> None:
    for name in SANDBOX_FILES:
        write_sandbox(name, [])


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def next_id(prefix: str, existing: list[str]) -> str:
    """next_id("INV-MG", ["INV-MG-001"]) -> "INV-MG-002". Ids are never reused."""
    nums = [int(m.group(1)) for i in existing if (m := re.fullmatch(rf"{re.escape(prefix)}-(\d+)", i))]
    return f"{prefix}-{max(nums, default=0) + 1:03d}"


def audit(event: str, **details: Any) -> None:
    append_sandbox("audit_log.json", {"timestamp": now(), "event": event, **details})


# ---------- domain reads ----------

def get_plans() -> dict[str, Plan]:
    """Original plans with sandbox amendments applied in order."""
    plans = {p["plan_id"]: dict(p) for p in _read(config.DATA_DIR / "billing_plans.json")}
    for amd in read_sandbox("plan_amendments.json"):
        plan = plans.get(amd["plan_id"])
        if plan is not None:
            plan.update(amd["change_set"])
            plan.setdefault("amended_by_sandbox", []).append(amd["amendment_id"])
    return {pid: Plan(**p) for pid, p in plans.items()}


def get_invoices() -> list[Invoice]:
    data = [Invoice(**i) for i in _read(config.DATA_DIR / "invoices.json")]
    sandbox = [Invoice(**{**i, "source": "sandbox"}) for i in read_sandbox("invoices.json")]
    return data + sandbox


def get_credit_memos() -> list[CreditMemo]:
    data = [CreditMemo(**m) for m in _read(config.DATA_DIR / "credit_memos.json")]
    sandbox = [CreditMemo(**{**m, "source": "sandbox"}) for m in read_sandbox("credit_memos.json")]
    return data + sandbox


def get_exchange_rates() -> list[dict]:
    return _read(config.DATA_DIR / "exchange_rates.json")
