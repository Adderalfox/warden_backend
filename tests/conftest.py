import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth.db import reset_db  # noqa: E402
from app.core.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """Every test gets its own user DB and (unless it opts in) no login requirement."""
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    reset_db()
    yield
    reset_db()
