"""Anti cross-tenant: /avulsos/pendentes não usa claim stale (ex.: Giro após login RUB_TEST1)."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.security import HTTPAuthorizationCredentials

import auth as auth_mod
from avulso_campos_routes import _sub_base, get_avulsos_pendentes
from base import _resolve_user_sub_base


def _db_motoboy_single_link(*, claim_not_linked: bool = True):
    """
    Mock mínimo do fluxo _resolve_user_sub_base para role=4:
    claim (ex. Giro) sem vínculo → único MotoboySubBase = RUB_TEST1.
    """
    db = MagicMock()
    calls = {"scalar": 0}

    def db_scalar(_stmt):
        calls["scalar"] += 1
        if claim_not_linked and calls["scalar"] == 1:
            return None  # claim não vinculada em MotoboySubBase
        return SimpleNamespace(id_motoboy=10)

    links = MagicMock()
    links.all.return_value = ["RUB_TEST1"]
    db.scalar = db_scalar
    db.scalars.return_value = links
    db.get.return_value = SimpleNamespace(sub_base="Giro")
    return db


def test_resolve_user_sub_base_ignores_stale_giro_when_only_rub_test1():
    db = _db_motoboy_single_link()
    user = SimpleNamespace(id=5, role=4, motoboy_id=10, sub_base="Giro")
    assert _resolve_user_sub_base(db, user) == "RUB_TEST1"


def test_avulso_sub_base_helper_ignores_stale_giro_claim():
    db = _db_motoboy_single_link()
    user = SimpleNamespace(id=5, role=4, motoboy_id=10, sub_base="Giro")
    assert _sub_base(db, user) == "RUB_TEST1"


def test_get_avulsos_pendentes_filters_by_resolved_sub_base(monkeypatch):
    """Claim Giro + vínculo só RUB_TEST1 → list_pendentes recebe RUB_TEST1."""
    captured = {}

    def fake_list_pendentes(db, *, sub_base, **kwargs):
        captured["sub_base"] = sub_base
        return SimpleNamespace(total=0, modo="todos", ambiguo=False, mensagem=None, rows=[])

    monkeypatch.setattr("avulso_campos_routes.list_pendentes", fake_list_pendentes)
    monkeypatch.setattr("avulso_campos_routes.resolve_campos_ativos", lambda *a, **k: [])

    user = SimpleNamespace(role=4, sub_base="Giro", motoboy_id=10, id=5)
    out = get_avulsos_pendentes(
        q=None,
        identificadores=None,
        todos_do_dia=True,
        limit=50,
        offset=0,
        db=_db_motoboy_single_link(),
        current_user=user,
    )
    assert captured["sub_base"] == "RUB_TEST1"
    assert out["total"] == 0


def test_coleta_operacional_sub_base_ignores_stale_giro_claim():
    from coleta_operacional_routes import _sub_base as coleta_sub_base

    db = _db_motoboy_single_link()
    user = SimpleNamespace(id=5, role=4, motoboy_id=10, sub_base="Giro")
    assert coleta_sub_base(db, user) == "RUB_TEST1"


@pytest.mark.asyncio
async def test_get_current_user_prefers_bearer_over_cookie(monkeypatch):
    decoded = {}

    def fake_decode(token, *a, **k):
        decoded["token"] = token
        return {
            "uid": 7,
            "role": 4,
            "motoboy_id": 99,
            "owner_ativo": True,
            "username": "moto",
            "sub_base": "RUB_TEST1",
            "ignorar_coleta": False,
        }

    monkeypatch.setattr(auth_mod.jwt, "decode", fake_decode)
    monkeypatch.setattr(
        auth_mod,
        "_hydrate_motoboy_permissions_from_db",
        lambda db, user, **kw: user,
    )

    request = MagicMock()
    request.cookies.get.return_value = "cookie-token-giro-root"
    request.state = SimpleNamespace()
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="bearer-token-motoboy",
    )

    user = await auth_mod.get_current_user(
        request,
        MagicMock(),
        credentials=credentials,
        db=MagicMock(),
    )
    assert decoded["token"] == "bearer-token-motoboy"
    assert user.role == 4
