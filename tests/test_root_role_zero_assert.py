"""Garante que role=0 (root) não é tratado como ausente por falsy em Python."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret")

from types import SimpleNamespace

from auth import _coerce_role_int
from envio_proprio_routes import _assert_operacao_etiqueta
from owner_routes import _assert_role_01
from politicas_routes import _assert_admin


def test_coerce_role_int_preserva_zero():
    assert _coerce_role_int(0) == 0
    assert _coerce_role_int("0") == 0
    assert _coerce_role_int(None) is None


def test_assert_admin_aceita_root_role_zero():
    _assert_admin(SimpleNamespace(role=0))
    _assert_admin(SimpleNamespace(role="0"))


def test_assert_role_01_aceita_root_role_zero():
    _assert_role_01(SimpleNamespace(role=0))


def test_assert_operacao_etiqueta_aceita_root_role_zero():
    _assert_operacao_etiqueta(SimpleNamespace(role=0))


def test_role_falsy_or_menos_um_quebrava_root():
    """Documenta o bug: `0 or -1` vira -1 e bloqueava root."""
    buggy = int(getattr(SimpleNamespace(role=0), "role", -1) or -1)
    assert buggy == -1
    fixed = _coerce_role_int(getattr(SimpleNamespace(role=0), "role", None))
    assert fixed == 0
