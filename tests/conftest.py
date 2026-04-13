"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

import pymh.workspace as ws


@pytest.fixture(autouse=True)
def isolated_mh_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Generator[Path, None, None]:
    """Redirect ~/.mh/ and ~/.claude/ to temp directories for all tests."""
    fake_root = tmp_path / ".mh"
    fake_claude = tmp_path / ".claude"
    monkeypatch.setattr(ws, "MH_ROOT", fake_root)
    monkeypatch.setattr(ws, "CLAUDE_HOME", fake_claude)
    yield fake_root
