"""Menu 'Gerar Etiqueta' para Base e Sub-base; envio próprio continua só-Base na API."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret")

from routes_ui import menu_for_role


def _labels(menu) -> list[str]:
    out: list[str] = []
    for section in menu:
        for item in section.get("items") or []:
            out.append(item.get("label") or "")
    return out


def test_menu_gerar_etiqueta_visivel_subbase_admin():
    assert "Gerar Etiqueta" in _labels(menu_for_role(1, tipo_owner="subbase"))


def test_menu_gerar_etiqueta_visivel_subbase_operador():
    assert "Gerar Etiqueta" in _labels(menu_for_role(2, tipo_owner="subbase"))


def test_menu_gerar_etiqueta_visivel_base_admin():
    assert "Gerar Etiqueta" in _labels(menu_for_role(1, tipo_owner="base"))


def test_menu_gerar_etiqueta_visivel_base_operador():
    assert "Gerar Etiqueta" in _labels(menu_for_role(2, tipo_owner="base"))


def test_menu_gerar_etiqueta_visivel_root_subbase():
    assert "Gerar Etiqueta" in _labels(menu_for_role(0, tipo_owner="subbase"))


def test_menu_gerar_etiqueta_visivel_root_base():
    assert "Gerar Etiqueta" in _labels(menu_for_role(0, tipo_owner="base"))


def test_menu_autenticacao_oculto_subbase_admin():
    """Autenticação continua base_only para admin; não mistura com Gerar Etiqueta."""
    assert "Autenticação" not in _labels(menu_for_role(1, tipo_owner="subbase"))


def test_menu_autenticacao_visivel_base_admin():
    assert "Autenticação" in _labels(menu_for_role(1, tipo_owner="base"))
