"""Testes leves do fast path de data operacional no scan."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch


def test_ctx_data_operacional_fast_path_hoje():
    from mobile_entregas_routes import _ctx_data_operacional_saida

    hoje = date(2026, 7, 22)
    saida = SimpleNamespace(data=hoje, timestamp=datetime(2026, 7, 22, 10, 0, 0), id_saida=1)

    with patch("mobile_entregas_routes._hoje_operacional", return_value=hoje):
        with patch("mobile_entregas_routes.carregar_contexto_operacional") as mock_ctx:
            assert _ctx_data_operacional_saida(None, saida) == hoje
            mock_ctx.assert_not_called()


def test_ctx_data_operacional_carrega_historico_quando_data_antiga():
    from mobile_entregas_routes import _ctx_data_operacional_saida

    hoje = date(2026, 7, 22)
    saida = SimpleNamespace(
        data=date(2026, 7, 20),
        timestamp=datetime(2026, 7, 20, 10, 0, 0),
        id_saida=9,
    )
    ctx = SimpleNamespace(operacional_ts=datetime(2026, 7, 20, 11, 0, 0))

    with patch("mobile_entregas_routes._hoje_operacional", return_value=hoje):
        with patch(
            "mobile_entregas_routes.carregar_contexto_operacional",
            return_value={9: ctx},
        ) as mock_ctx:
            assert _ctx_data_operacional_saida(None, saida) == date(2026, 7, 20)
            mock_ctx.assert_called_once()
