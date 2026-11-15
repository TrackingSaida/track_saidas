"""Menu 'Conferência de saída' depende da flag de conferência, não de sessões."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret")

from routes_ui import menu_for_role


def _hrefs(menu) -> set[str]:
    out: set[str] = set()
    for section in menu:
        for item in section.get("items") or []:
            href = item.get("href")
            if href:
                out.add(href)
    return out


def test_menu_conferencia_oculta_sem_flag():
    hrefs = _hrefs(menu_for_role(1, conferencia_saida=False))
    assert "tracking-conferencia-saida.html" not in hrefs


def test_menu_conferencia_visivel_com_flag():
    hrefs = _hrefs(menu_for_role(1, conferencia_saida=True))
    assert "tracking-conferencia-saida.html" in hrefs


def test_menu_conferencia_visivel_operador():
    hrefs = _hrefs(menu_for_role(2, conferencia_saida=True))
    assert "tracking-conferencia-saida.html" in hrefs
