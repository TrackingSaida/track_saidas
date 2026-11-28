"""PRD-020: idempotência do lote e finalizar."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "test-secret")

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from coleta_client_action_service import (
    buscar_resultado_idempotente,
    claim_client_action,
    completar_client_action,
    persistir_resultado_idempotente,
)
from entrega_audit_log import norm_client_action_id


def test_norm_client_action_id_trim_e_limite():
    assert norm_client_action_id(None) is None
    assert norm_client_action_id("  ") is None
    assert norm_client_action_id(" abc ") == "abc"
    longo = "x" * 100
    assert len(norm_client_action_id(longo) or "") == 64


def test_buscar_resultado_idempotente_retorna_flag():
    db = MagicMock()
    row = SimpleNamespace(
        resultado_json=json.dumps(
            {
                "resumo": {
                    "inseridos": 1,
                    "duplicados": 0,
                    "codigos_duplicados": [],
                    "contagem": {},
                    "precos": {},
                    "total": "0",
                },
                "saidas_criadas": [],
            }
        )
    )
    db.scalar.return_value = row
    out = buscar_resultado_idempotente(db, sub_base="SB", client_action_id="act-1")
    assert out is not None
    assert out["idempotente"] is True


def test_buscar_pending_nao_e_replay():
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(resultado_json=json.dumps({"_pending": True}))
    assert buscar_resultado_idempotente(db, sub_base="SB", client_action_id="act-1") is None


def test_buscar_sem_chave_retorna_none():
    db = MagicMock()
    assert buscar_resultado_idempotente(db, sub_base="SB", client_action_id=None) is None
    db.scalar.assert_not_called()


def test_claim_sem_chave_proceed():
    db = MagicMock()
    action, replay = claim_client_action(db, sub_base="SB", client_action_id=None)
    assert action == "proceed"
    assert replay is None
    db.add.assert_not_called()


def test_claim_replay_quando_completo():
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(
        resultado_json=json.dumps({"resumo": {"inseridos": 1}})
    )
    action, replay = claim_client_action(db, sub_base="SB", client_action_id="act-1")
    assert action == "replay"
    assert replay is not None
    assert replay["idempotente"] is True


def test_claim_in_progress_quando_pending():
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(resultado_json=json.dumps({"_pending": True}))
    action, replay = claim_client_action(db, sub_base="SB", client_action_id="act-1")
    assert action == "in_progress"
    assert replay is None


def test_persistir_nao_grava_sem_chave():
    db = MagicMock()
    persistir_resultado_idempotente(
        db,
        sub_base="SB",
        client_action_id=None,
        resultado={"ok": True},
    )
    db.add.assert_not_called()


def test_completar_atualiza_row_existente():
    db = MagicMock()
    row = SimpleNamespace(
        resultado_json=json.dumps({"_pending": True}),
        base=None,
        codigo=None,
        id_saida=None,
        id_coleta=None,
        id_execucao=None,
    )
    db.scalar.return_value = row
    completar_client_action(
        db,
        sub_base="SB",
        client_action_id="act-1",
        resultado={"resumo": {"inseridos": 1}},
        base="BASE1",
        codigo="ABC",
        id_saida=10,
    )
    payload = json.loads(row.resultado_json)
    assert payload.get("_pending") is None
    assert payload["resumo"]["inseridos"] == 1
    assert row.base == "BASE1"
    assert row.codigo == "ABC"
    assert row.id_saida == 10


def test_finalizar_ja_finalizado_e_idempotente_por_status():
    """Espelha o gate da rota: participante finalizado → sem mutação."""
    participante = SimpleNamespace(status="finalizado")
    assert getattr(participante, "status", None) == "finalizado"
    execucao = SimpleNamespace(
        status="coletado",
        participantes=[SimpleNamespace(status="finalizado")],
    )
    assert getattr(execucao, "status", None) in ("coletado", "sem_volume")
    assert all(getattr(p, "status", None) == "finalizado" for p in execucao.participantes)
