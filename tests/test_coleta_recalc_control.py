"""Controle de adiamento do recalcular_coleta (finalizar-lote)."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "pytest-secret-key")

from coleta_recalc_control import defer_recalcular_coleta, should_skip_recalcular_coleta


def test_defer_recalcular_coleta_context():
    assert should_skip_recalcular_coleta() is False
    with defer_recalcular_coleta():
        assert should_skip_recalcular_coleta() is True
    assert should_skip_recalcular_coleta() is False
