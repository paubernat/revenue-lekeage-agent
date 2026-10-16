"""The only module that touches disk. Reads data/ (read-only) merged with sandbox/
(writable), so applied actions are visible to the next investigation."""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

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
# Loading is tolerant: an invalid record or a repeated id is skipped and reported by
# load_issues() (a data_quality finding) instead of crashing the whole analysis.

def _build(raws: list[tuple[dict, str]], model, id_field: str, file: str) -> tuple[list, list[dict]]:
    records, issues, seen = [], [], set()
    for raw, source in raws:
        rid = raw.get(id_field, "<no id>")
        if rid in seen:
            issues.append({"file": file, "record_id": rid, "problem": f"duplicate {id_field}; later copy ignored"})
            continue
        try:
            records.append(model(**{**raw, **({"source": source} if "source" in model.model_fields else {})}))
            seen.add(rid)
        except ValidationError as e:
            err = e.errors()[0]
            field = ".".join(str(x) for x in err["loc"])
            issues.append({"file": file, "record_id": rid, "problem": f"{field}: {err['msg']}; record ignored"})
    return records, issues


def _plans_and_issues() -> tuple[dict[str, Plan], list[dict]]:
    raws = [dict(p) for p in _read(config.DATA_DIR / "billing_plans.json")]
    for amd in read_sandbox("plan_amendments.json"):
        for plan in raws:
            if plan.get("plan_id") == amd["plan_id"]:
                plan.update(amd["change_set"])
                plan.setdefault("amended_by_sandbox", []).append(amd["amendment_id"])
    plans, issues = _build([(p, "data") for p in raws], Plan, "plan_id", "billing_plans.json")
    return {p.plan_id: p for p in plans}, issues


def _invoices_and_issues() -> tuple[list[Invoice], list[dict]]:
    raws = [(i, "data") for i in _read(config.DATA_DIR / "invoices.json")]
    raws += [(i, "sandbox") for i in read_sandbox("invoices.json")]
    return _build(raws, Invoice, "invoice_id", "invoices.json")


def _memos_and_issues() -> tuple[list[CreditMemo], list[dict]]:
    raws = [(m, "data") for m in _read(config.DATA_DIR / "credit_memos.json")]
    raws += [(m, "sandbox") for m in read_sandbox("credit_memos.json")]
    return _build(raws, CreditMemo, "memo_id", "credit_memos.json")


def get_plans() -> dict[str, Plan]:
    """Original plans with sandbox amendments applied in order."""
    return _plans_and_issues()[0]


def get_invoices() -> list[Invoice]:
    return _invoices_and_issues()[0]


def get_credit_memos() -> list[CreditMemo]:
    return _memos_and_issues()[0]


def load_issues() -> list[dict]:
    return _plans_and_issues()[1] + _invoices_and_issues()[1] + _memos_and_issues()[1]


def get_exchange_rates() -> list[dict]:
    return _read(config.DATA_DIR / "exchange_rates.json")
