"""Central settings: paths, model, and the analysis as-of date."""
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DATASETS = {"Original": ROOT / "data", "Edge cases": ROOT / "data_edge_cases"}
_SANDBOX_ROOT = Path(os.getenv("SANDBOX_DIR", ROOT / "sandbox"))
SANDBOXES = {"Original": _SANDBOX_ROOT, "Edge cases": _SANDBOX_ROOT / "edge_cases"}
DATA_DIR = DATASETS["Original"]
SANDBOX_DIR = SANDBOXES["Original"]

# An invoice issued up to this many days before a period starts may be early billing for it.
EARLY_BILLING_DAYS = 10
# Late/early reassignment only for invoices worth at least this share of the period amount
# (so a small split payment or a refund stays in its own period).
REASSIGN_MIN_SHARE = 0.5

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
# USD per million tokens, only for the sidebar cost estimate. Check current pricing for your model.
PRICE_IN_PER_M = float(os.getenv("PRICE_IN_PER_M", 3))
PRICE_OUT_PER_M = float(os.getenv("PRICE_OUT_PER_M", 15))

# Only billing periods that started on or before this date are considered "due".
DEFAULT_AS_OF = date(2025, 12, 31)
_as_of = DEFAULT_AS_OF


def get_as_of() -> date:
    return _as_of


def set_as_of(value: date) -> None:
    global _as_of
    _as_of = value


def set_data_dir(name: str) -> None:
    global DATA_DIR, SANDBOX_DIR
    DATA_DIR, SANDBOX_DIR = DATASETS[name], SANDBOXES[name]
