"""Rótulo de ação de ocorrência inclui motivo quando disponível."""

from __future__ import annotations

from types import SimpleNamespace

from saida_operacional_pure import (
    ROTULOS_ACAO,
    rotulo_acao_ocorrencia_com_motivo,
)
from saidas_listar_service import build_operacional_ctx_from_historico_rows


def test_fallback_sem_motivo_usa_ocorrencia():
    assert ROTULOS_ACAO["ausente"] == "Registrou ocorrência"
    assert ROTULOS_ACAO["ausente_lote"] == "Registrou ocorrência em lote"
    assert rotulo_acao_ocorrencia_com_motivo("ausente") == "Registrou ocorrência"
    assert rotulo_acao_ocorrencia_com_motivo("ausente_lote") == "Registrou ocorrência em lote"
    assert rotulo_acao_ocorrencia_com_motivo("ausente", motivo_ocorrencia="  ") == "Registrou ocorrência"


def test_com_motivo_unitario():
    assert (
        rotulo_acao_ocorrencia_com_motivo("ausente", motivo_ocorrencia="Recusado")
        == "Registrou: Recusado"
    )
    assert (
        rotulo_acao_ocorrencia_com_motivo(
            "ausente", motivo_ocorrencia="  Cliente ausente  "
        )
        == "Registrou: Cliente ausente"
    )


def test_com_motivo_lote():
    assert (
        rotulo_acao_ocorrencia_com_motivo(
            "ausente_lote", motivo_ocorrencia="Endereço não encontrado"
        )
        == "Registrou em lote: Endereço não encontrado"
    )


def test_outros_eventos_ignoram_motivo():
    assert (
        rotulo_acao_ocorrencia_com_motivo("entregue", motivo_ocorrencia="Recusado")
        == "Finalizou entrega"
    )


def test_listagem_propaga_motivo_no_acao_label():
    hist = SimpleNamespace(
        id_saida=10,
        evento="ausente",
        timestamp=None,
        user_id=1,
        motoboy_id_anterior=None,
        payload='{"motivo_ocorrencia": "Recusado"}',
    )
    ctx = build_operacional_ctx_from_historico_rows([10], [hist], {1: "boy"})
    assert ctx[10].acao_label == "Registrou: Recusado"


def test_listagem_sem_motivo_usa_fallback():
    hist = SimpleNamespace(
        id_saida=11,
        evento="ausente",
        timestamp=None,
        user_id=1,
        motoboy_id_anterior=None,
        payload=None,
    )
    ctx = build_operacional_ctx_from_historico_rows([11], [hist], {1: "boy"})
    assert ctx[11].acao_label == "Registrou ocorrência"
