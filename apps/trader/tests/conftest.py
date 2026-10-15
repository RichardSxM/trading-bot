"""Shared setup for the trader tests."""

import pytest


@pytest.fixture(autouse=True)
def total_loss_since(monkeypatch):
    """``RISK_TOTAL_LOSS_SINCE`` has no default (spec 0010 AC-21); Compose sets it, so the tests do too."""
    monkeypatch.setenv("RISK_TOTAL_LOSS_SINCE", "2026-10-01")
