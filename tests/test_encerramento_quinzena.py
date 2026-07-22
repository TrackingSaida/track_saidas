"""Testes do encerramento por quinzena (janela viva + limite por requisição)."""
from __future__ import annotations

import unittest
from datetime import date, datetime
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch

from encerramento_quinzena_service import (
    STATUS_SAIU_PARA_ENTREGA,
    periodo_janela_viva_quinzenas,
    run_encerrar_pendentes_quinzena,
)


class TestJanelaViva(unittest.TestCase):
    def test_janela_primeira_quinzena_inclui_segunda_do_mes_anterior(self):
        inicio, fim = periodo_janela_viva_quinzenas(date(2026, 7, 10))
        self.assertEqual(inicio, date(2026, 6, 16))
        self.assertEqual(fim, date(2026, 7, 10))

    def test_janela_segunda_quinzena_inclui_primeira_do_mes(self):
        inicio, fim = periodo_janela_viva_quinzenas(date(2026, 7, 20))
        self.assertEqual(inicio, date(2026, 7, 1))
        self.assertEqual(fim, date(2026, 7, 20))

    def test_janela_janeiro_cruza_dezembro(self):
        inicio, fim = periodo_janela_viva_quinzenas(date(2026, 1, 5))
        self.assertEqual(inicio, date(2025, 12, 16))
        self.assertEqual(fim, date(2026, 1, 5))


def _saida(
    id_saida: int,
    *,
    status: str = STATUS_SAIU_PARA_ENTREGA,
    sub_base: str = "DG EXPRESS",
    data_op: Optional[date] = date(2026, 5, 1),
):
    return SimpleNamespace(
        id_saida=id_saida,
        status=status,
        sub_base=sub_base,
        codigo=f"BR{id_saida:05d}",
        data=data_op,
        timestamp=datetime.combine(data_op, datetime.min.time()) if data_op else None,
        motoboy_id=10,
        id_coleta=100,
    )


class TestEncerramentoBatchLimit(unittest.TestCase):
    def _mock_db(self, saidas):
        db = MagicMock()
        db.scalars.return_value.all.return_value = saidas
        db.execute = MagicMock()
        db.add = MagicMock()
        db.commit = MagicMock()
        db.expire_all = MagicMock()
        return db

    def _ctx_ok(self, ids):
        # timestamp operacional antigo (antes da janela viva)
        return {
            sid: SimpleNamespace(
                operacional_ts=datetime(2026, 5, 1, 12, 0, 0),
                ultimo_evento_ts=datetime(2026, 5, 1, 12, 0, 0),
                removido_sem_inicio_ativo=False,
            )
            for sid in ids
        }

    @patch("encerramento_quinzena_service.deve_excluir_saida_operacional", return_value=False)
    @patch("encerramento_quinzena_service.carregar_contexto_operacional")
    def test_batch_size_50_nunca_atualiza_mais_de_50(self, mock_ctx, _mock_excluir):
        saidas = [_saida(i) for i in range(1, 121)]  # 120 elegíveis
        mock_ctx.side_effect = lambda _db, ids: self._ctx_ok(ids)
        db = self._mock_db(saidas)

        result = run_encerrar_pendentes_quinzena(
            db,
            ref=date(2026, 7, 20),
            dry_run=False,
            batch_size=50,
            sub_base="DG EXPRESS",
        )

        self.assertEqual(result.elegiveis, 120)
        self.assertEqual(result.atualizados, 50)
        self.assertEqual(result.restantes, 70)
        self.assertTrue(result.tem_mais)
        self.assertEqual(result.batch_size, 50)
        self.assertEqual(db.commit.call_count, 1)
        self.assertEqual(db.execute.call_count, 1)
        # 50 históricos adicionados (um por id atualizado)
        self.assertEqual(db.add.call_count, 50)
        # Contagem do lote desta requisição
        self.assertEqual(sum(result.por_sub_base.values()), 50)
        self.assertLessEqual(len(result.sample_ids), 20)

    @patch("encerramento_quinzena_service.deve_excluir_saida_operacional", return_value=False)
    @patch("encerramento_quinzena_service.carregar_contexto_operacional")
    def test_dry_run_nao_altera(self, mock_ctx, _mock_excluir):
        saidas = [_saida(i) for i in range(1, 61)]
        mock_ctx.side_effect = lambda _db, ids: self._ctx_ok(ids)
        db = self._mock_db(saidas)

        result = run_encerrar_pendentes_quinzena(
            db,
            ref=date(2026, 7, 20),
            dry_run=True,
            batch_size=50,
        )

        self.assertEqual(result.elegiveis, 60)
        self.assertEqual(result.atualizados, 0)
        self.assertEqual(result.restantes, 60)
        self.assertTrue(result.tem_mais)
        db.execute.assert_not_called()
        db.commit.assert_not_called()
        db.add.assert_not_called()

    @patch("encerramento_quinzena_service.deve_excluir_saida_operacional", return_value=False)
    @patch("encerramento_quinzena_service.carregar_contexto_operacional")
    def test_nao_encera_mais_que_elegiveis(self, mock_ctx, _mock_excluir):
        saidas = [_saida(i) for i in range(1, 11)]
        mock_ctx.side_effect = lambda _db, ids: self._ctx_ok(ids)
        db = self._mock_db(saidas)

        result = run_encerrar_pendentes_quinzena(
            db,
            ref=date(2026, 7, 20),
            dry_run=False,
            batch_size=50,
        )

        self.assertEqual(result.elegiveis, 10)
        self.assertEqual(result.atualizados, 10)
        self.assertEqual(result.restantes, 0)
        self.assertFalse(result.tem_mais)
        self.assertEqual(db.commit.call_count, 1)

    @patch("encerramento_quinzena_service.deve_excluir_saida_operacional", return_value=False)
    @patch("encerramento_quinzena_service.carregar_contexto_operacional")
    def test_preserva_escopo_sub_base_nos_selecionados(self, mock_ctx, _mock_excluir):
        saidas = [_saida(i, sub_base="DG EXPRESS") for i in range(1, 6)]
        mock_ctx.side_effect = lambda _db, ids: self._ctx_ok(ids)
        db = self._mock_db(saidas)

        result = run_encerrar_pendentes_quinzena(
            db,
            ref=date(2026, 7, 20),
            dry_run=False,
            batch_size=50,
            sub_base="DG EXPRESS",
        )

        self.assertEqual(result.por_sub_base, {"DG EXPRESS": 5})
        self.assertEqual(result.atualizados, 5)


if __name__ == "__main__":
    unittest.main()
