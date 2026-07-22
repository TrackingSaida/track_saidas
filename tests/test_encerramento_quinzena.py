"""Testes do helper de janela viva (2 quinzenas)."""
from datetime import date

from encerramento_quinzena_service import periodo_janela_viva_quinzenas


def test_janela_primeira_quinzena_inclui_segunda_do_mes_anterior():
    inicio, fim = periodo_janela_viva_quinzenas(date(2026, 7, 10))
    assert inicio == date(2026, 6, 16)
    assert fim == date(2026, 7, 10)


def test_janela_segunda_quinzena_inclui_primeira_do_mes():
    inicio, fim = periodo_janela_viva_quinzenas(date(2026, 7, 20))
    assert inicio == date(2026, 7, 1)
    assert fim == date(2026, 7, 20)


def test_janela_janeiro_cruza_dezembro():
    inicio, fim = periodo_janela_viva_quinzenas(date(2026, 1, 5))
    assert inicio == date(2025, 12, 16)
    assert fim == date(2026, 1, 5)
