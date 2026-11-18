"""Shared pytest fixtures for agent-watch."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_run_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep launcher state inside tmp_path so tests never touch %LOCALAPPDATA%."""
    monkeypatch.setenv("AGENT_WATCH_RUN_DIR", str(tmp_path / "run"))


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the path to the test fixtures directory."""
    return Path(__file__).parent / "fixtures"
