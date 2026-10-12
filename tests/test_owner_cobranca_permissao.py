"""Cobrança de Owners: acesso exclusivo do root (role 0)."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret")

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from owner_cobranca_routes import require_root, router
from routes_ui import menu_for_role

HREF = "tracking-cobranca-owners.html"


def _hrefs(role: int) -> set[str]:
    return {i["href"] for s in menu_for_role(role) for i in s["items"]}


@pytest.mark.parametrize("role", [0, "0"])
def test_root_passa(role):
    user = SimpleNamespace(role=role)
    assert require_root(user) is user


@pytest.mark.parametrize("role", [1, 2, 3, 4, None, "", "1"])
def test_demais_perfis_recebem_403(role):
    with pytest.raises(HTTPException) as exc:
        require_root(SimpleNamespace(role=role))
    assert exc.value.status_code == 403


def test_todas_as_rotas_exigem_root():
    deps = {d.dependency for d in router.dependencies}
    assert require_root in deps
    assert router.prefix == "/admin/cobranca-owners"
    assert len(router.routes) >= 19


def test_menu_so_para_root():
    assert HREF in _hrefs(0)
    for role in (1, 2, 3):
        assert HREF not in _hrefs(role)


def test_financeiro_continua_para_admin():
    assert "tracking-contabilidade.html" in _hrefs(1)
