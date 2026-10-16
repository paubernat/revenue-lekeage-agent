"""Central settings: paths, model, and the analysis as-of date."""
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
SANDBOX_DIR = Path(os.getenv("SANDBOX_DIR", ROOT / "sandbox"))

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

# Only billing periods that started on or before this date are considered "due".
DEFAULT_AS_OF = date(2025, 12, 31)
_as_of = DEFAULT_AS_OF


def get_as_of() -> date:
    return _as_of


def set_as_of(value: date) -> None:
    global _as_of
    _as_of = value
