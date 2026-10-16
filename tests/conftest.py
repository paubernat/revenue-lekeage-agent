import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from core import repository  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_sandbox(tmp_path, monkeypatch):
    """Every test gets an empty sandbox so the real one is never touched."""
    monkeypatch.setattr(config, "SANDBOX_DIR", tmp_path / "sandbox")
    config.set_as_of(date(2025, 12, 31))
    repository.reset_sandbox()
    yield
