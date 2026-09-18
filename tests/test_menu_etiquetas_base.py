"""Menu 'Gerar Etiqueta' só para Owner Base; Root continua vendo em qualquer tenant."""
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


def test_menu_gerar_etiqueta_oculto_subbase_admin():
    assert "Gerar Etiqueta" not in _labels(menu_for_role(1, tipo_owner="subbase"))


def test_menu_gerar_etiqueta_oculto_subbase_operador():
    assert "Gerar Etiqueta" not in _labels(menu_for_role(2, tipo_owner="subbase"))


def test_menu_gerar_etiqueta_visivel_base_admin():
    assert "Gerar Etiqueta" in _labels(menu_for_role(1, tipo_owner="base"))


def test_menu_gerar_etiqueta_visivel_base_operador():
    assert "Gerar Etiqueta" in _labels(menu_for_role(2, tipo_owner="base"))


def test_menu_gerar_etiqueta_visivel_root_subbase():
    """Root vê o item mesmo em tenant subbase (create da API ainda exige Base)."""
    assert "Gerar Etiqueta" in _labels(menu_for_role(0, tipo_owner="subbase"))


def test_menu_gerar_etiqueta_visivel_root_base():
    assert "Gerar Etiqueta" in _labels(menu_for_role(0, tipo_owner="base"))
