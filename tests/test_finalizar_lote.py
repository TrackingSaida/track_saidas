"""Testes unitários para finalização em lote (sem banco real)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pedido_campos_obrigatorios_service import format_bloqueio_motivo
from mobile_entregas_routes import (
    FinalizarLoteBody,
    _validar_saida_para_finalizacao_lote,
    finalizar_lote,
)
from saidas_routes import (
    STATUS_SAIU_PARA_ENTREGA,
    STATUS_EM_ROTA,
    STATUS_ENTREGUE,
    STATUS_AUSENTE,
    STATUS_CANCELADO,
)


def _saida(
    id_saida: int = 1,
    motoboy_id: int = 10,
    sub_base: str = "SB1",
    status: str = STATUS_EM_ROTA,
    codigo: str = "BR001",
):
    return SimpleNamespace(
        id_saida=id_saida,
        motoboy_id=motoboy_id,
        sub_base=sub_base,
        status=status,
        codigo=codigo,
        data_hora_entrega=None,
    )


class TestFormatBloqueioMotivo(unittest.TestCase):
    def test_recebedor(self):
        self.assertEqual(format_bloqueio_motivo(["recebedor"]), "Exige nome do recebedor")

    def test_documento(self):
        self.assertEqual(format_bloqueio_motivo(["documento"]), "Exige documento")

    def test_foto(self):
        self.assertEqual(format_bloqueio_motivo(["foto"]), "Exige foto/comprovante")


class TestValidarSaidaLote(unittest.TestCase):
    def test_ok_em_rota(self):
        s = _saida(status=STATUS_EM_ROTA)
        self.assertIsNone(_validar_saida_para_finalizacao_lote(s, "entregue", 10, "SB1"))

    def test_ok_saiu_para_entrega(self):
        s = _saida(status=STATUS_SAIU_PARA_ENTREGA)
        self.assertIsNone(_validar_saida_para_finalizacao_lote(s, "entregue", 10, "SB1"))

    def test_outro_motoboy(self):
        s = _saida(motoboy_id=99)
        motivo = _validar_saida_para_finalizacao_lote(s, "entregue", 10, "SB1")
        self.assertIn("não pertence", motivo or "")

    def test_ja_finalizado(self):
        s = _saida(status=STATUS_ENTREGUE)
        self.assertEqual(
            _validar_saida_para_finalizacao_lote(s, "entregue", 10, "SB1"),
            "Pedido já finalizado",
        )

    def test_ausente_tentando_entregue(self):
        s = _saida(status=STATUS_AUSENTE)
        self.assertEqual(
            _validar_saida_para_finalizacao_lote(s, "entregue", 10, "SB1"),
            "Pedido já marcado como ausente",
        )

    def test_cancelado(self):
        s = _saida(status=STATUS_CANCELADO)
        self.assertEqual(
            _validar_saida_para_finalizacao_lote(s, "entregue", 10, "SB1"),
            "Pedido já finalizado",
        )


class TestFinalizarLoteEndpoint(unittest.TestCase):
    def _user(self):
        return SimpleNamespace(id=1, motoboy_id=10, sub_base="SB1", role=4)

    def test_ausente_sem_motivo_422(self):
        db = MagicMock()
        user = self._user()
        body = FinalizarLoteBody(ids=[1, 2], acao="ausente")
        with self.assertRaises(Exception) as ctx:
            from fastapi import HTTPException

            try:
                finalizar_lote(body, db, user)
            except HTTPException as e:
                raise e
        self.assertEqual(ctx.exception.status_code, 422)

    @patch("mobile_entregas_routes.validate_campos_obrigatorios_conclusao", return_value=[])
    @patch("mobile_entregas_routes._carregar_details_por_saida_ids", return_value={})
    def test_tres_entregues_sem_campos(self, _details, _validate):
        db = MagicMock()
        user = self._user()
        saidas = [_saida(id_saida=i, codigo=f"BR{i}") for i in (1, 2, 3)]

        def get_saida(pk):
            return next((s for s in saidas if s.id_saida == pk), None)

        db.get.side_effect = get_saida
        db.commit = MagicMock()
        db.refresh = MagicMock()
        db.rollback = MagicMock()

        body = FinalizarLoteBody(ids=[1, 2, 3], acao="entregue")
        resp = finalizar_lote(body, db, user)
        self.assertEqual(len(resp.finalizados), 3)
        self.assertEqual(len(resp.bloqueados), 0)
        self.assertEqual(db.commit.call_count, 3)

    @patch("mobile_entregas_routes.validate_campos_obrigatorios_conclusao")
    @patch("mobile_entregas_routes._carregar_details_por_saida_ids", return_value={})
    def test_parcial_um_bloqueado_recebedor(self, _details, validate_mock):
        def validate_side_effect(db, *, saida, contexto, detail, overrides=None):
            if saida.id_saida == 2:
                return ["recebedor"]
            return []

        validate_mock.side_effect = validate_side_effect
        db = MagicMock()
        user = self._user()
        saidas = [_saida(id_saida=i, codigo=f"BR{i}") for i in (1, 2, 3)]
        db.get.side_effect = lambda pk: next((s for s in saidas if s.id_saida == pk), None)
        db.commit = MagicMock()
        db.refresh = MagicMock()
        db.rollback = MagicMock()

        body = FinalizarLoteBody(ids=[1, 2, 3], acao="entregue")
        resp = finalizar_lote(body, db, user)
        self.assertEqual(len(resp.finalizados), 2)
        self.assertEqual(len(resp.bloqueados), 1)
        self.assertEqual(resp.bloqueados[0].id_saida, 2)
        self.assertIn("recebedor", resp.bloqueados[0].motivo.lower())

    def test_outro_sem_observacao_422(self):
        from fastapi import HTTPException

        db = MagicMock()
        user = self._user()
        motivo = SimpleNamespace(id=4, descricao="Outro", ativo=True)
        db.get.return_value = motivo
        body = FinalizarLoteBody(ids=[1], acao="ausente", motivo_id=4, observacao=None)
        with self.assertRaises(HTTPException) as ctx:
            finalizar_lote(body, db, user)
        self.assertEqual(ctx.exception.status_code, 422)

    @patch("mobile_entregas_routes._carregar_details_por_saida_ids", return_value={})
    def test_ja_entregue_bloqueado(self, _details):
        db = MagicMock()
        user = self._user()
        s = _saida(id_saida=5, status=STATUS_ENTREGUE, codigo="BR5")
        db.get.return_value = s
        body = FinalizarLoteBody(ids=[5], acao="entregue")
        resp = finalizar_lote(body, db, user)
        self.assertEqual(len(resp.finalizados), 0)
        self.assertEqual(len(resp.bloqueados), 1)
        self.assertEqual(resp.bloqueados[0].motivo, "Pedido já finalizado")

    @patch("mobile_entregas_routes.validate_campos_obrigatorios_conclusao", return_value=[])
    @patch("mobile_entregas_routes._carregar_details_por_saida_ids", return_value={})
    def test_historico_entregue_lote(self, _details, _validate):
        db = MagicMock()
        user = self._user()
        s = _saida(id_saida=7, status=STATUS_SAIU_PARA_ENTREGA)
        db.get.return_value = s
        added = []

        def track_add(obj):
            added.append(obj)

        db.add.side_effect = track_add
        db.commit = MagicMock()
        db.refresh = MagicMock()

        body = FinalizarLoteBody(ids=[7], acao="entregue")
        resp = finalizar_lote(body, db, user)
        self.assertEqual(len(resp.finalizados), 1)
        historicos = [o for o in added if getattr(o, "evento", None) == "entregue_lote"]
        self.assertEqual(len(historicos), 1)
        self.assertEqual(historicos[0].status_novo, STATUS_ENTREGUE)


if __name__ == "__main__":
    unittest.main()
