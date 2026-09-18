"""E-mail opcional em User: vazio → NULL, sem placeholder; Owner continua obrigatório no create."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret")

from types import SimpleNamespace

from fastapi import HTTPException

from owner_routes import OwnerCreate
from users_routes_updated import (
    UserOut,
    _deny_non_root_assigning_root,
    _normalize_optional_email,
    _user_to_out,
)


def test_normalize_email_vazio_vira_none():
    assert _normalize_optional_email(None) is None
    assert _normalize_optional_email("") is None
    assert _normalize_optional_email("   ") is None


def test_normalize_email_valido():
    assert _normalize_optional_email("  user@ex.com  ") == "user@ex.com"


def test_normalize_email_invalido():
    try:
        _normalize_optional_email("nao-e-email")
        raise AssertionError("esperava 422")
    except HTTPException as e:
        assert e.status_code == 422


def test_user_out_aceita_email_null():
    out = UserOut(id=1, email=None, username="op", contato="11999999999")
    assert out.email is None


def test_user_to_out_nao_inventa_placeholder():
    user = SimpleNamespace(
        id=10,
        email=None,
        username="moto1",
        contato="11988887777",
        status=True,
        sub_base="sb",
        nome="Ana",
        sobrenome="Silva",
        data_nascimento=None,
        role=4,
        coletador=False,
        motoboy=None,
        must_change_password=True,
    )
    out = _user_to_out(user)
    assert out.email is None
    assert "sem-email" not in (out.email or "")


def test_admin_nao_atribui_root():
    try:
        _deny_non_root_assigning_root(SimpleNamespace(role=1), 0)
        raise AssertionError("esperava 403")
    except HTTPException as e:
        assert e.status_code == 403


def test_root_pode_atribuir_root():
    _deny_non_root_assigning_root(SimpleNamespace(role=0), 0)


def test_owner_create_schema_aceita_email_opcional_no_body():
    body = OwnerCreate(sub_base="nova", email=None)
    assert body.email is None
