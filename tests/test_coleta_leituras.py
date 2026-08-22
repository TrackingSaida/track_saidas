"""Testes de consulta e remoção segura de leituras de coleta."""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from coleta_leituras_service import (
    avaliar_remocao,
    decode_cursor,
    encode_cursor,
    listar_leituras,
    remover_leitura,
    resumo_base_dia,
    totais_da_execucao,
)


def test_cursor_roundtrip():
    ts = datetime(2026, 8, 22, 15, 30, 0)
    cursor = encode_cursor(ts, 42)
    assert decode_cursor(cursor) == (ts, 42)


def test_cursor_invalido():
    with pytest.raises(HTTPException) as exc:
        decode_cursor("%%%")
    assert exc.value.status_code == 422


def test_totais_da_execucao_soma_participantes():
    execucao = SimpleNamespace(
        participantes=[
            SimpleNamespace(shopee=1, mercado_livre=2, avulso=0),
            SimpleNamespace(shopee=0, mercado_livre=1, avulso=3),
        ]
    )
    assert totais_da_execucao(execucao) == {
        "total": 7,
        "shopee": 1,
        "mercado_livre": 3,
        "avulso": 3,
    }
    assert totais_da_execucao(None)["total"] == 0


def _saida(**overrides):
    values = {
        "id_saida": 10,
        "id_coleta": 5,
        "status": "coletado",
        "motoboy_id": None,
        "data": date.today(),
        "timestamp": datetime.now(),
        "username": "operador",
        "codigo": "BR123",
        "servico": "Shopee",
        "base": "BASE-A",
        "sub_base": "SB",
        "is_grande": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _user(**overrides):
    values = {"id": 7, "role": 2, "username": "operador", "sub_base": "SB"}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_avaliar_remocao_operador_dono_ok():
    db = MagicMock()
    saida = _saida()

    def scalar_side_effect(*args, **kwargs):
        n = scalar_side_effect.n
        scalar_side_effect.n += 1
        if n == 0:
            return SimpleNamespace(user_id=7)  # dono hist
        if n == 1:
            return None  # cobranca
        return 0  # eventos posteriores

    scalar_side_effect.n = 0
    db.scalar.side_effect = scalar_side_effect
    db.get.return_value = SimpleNamespace(origem="codigo", participante_id=None)

    pode, motivo, dono = avaliar_remocao(db, saida=saida, current_user=_user())
    assert pode is True
    assert motivo is None
    assert dono == 7


def test_avaliar_remocao_operador_nao_remove_terceiro():
    db = MagicMock()
    saida = _saida(username="outro")
    db.scalar.side_effect = [
        SimpleNamespace(user_id=99),  # dono outro
        None,  # cobranca (coleta via get)
        0,
    ]
    db.get.return_value = SimpleNamespace(origem="codigo", participante_id=None)

    # First call path: _dono_user_id uses scalar, then get coleta
    # Re-setup more carefully
    def scalar_side_effect(stmt=None, *args, **kwargs):
        # Approximate: return hist first, then cobranca, then count
        call = scalar_side_effect.n
        scalar_side_effect.n += 1
        if call == 0:
            return SimpleNamespace(user_id=99)
        if call == 1:
            return None  # cobranca
        return 0

    scalar_side_effect.n = 0
    db = MagicMock()
    db.scalar.side_effect = scalar_side_effect
    db.get.return_value = SimpleNamespace(origem="codigo", participante_id=None)

    pode, motivo, _ = avaliar_remocao(db, saida=saida, current_user=_user(id=7, role=2))
    assert pode is False
    assert "admin" in (motivo or "").lower() or "operador" in (motivo or "").lower()


def test_avaliar_remocao_admin_pode_terceiro():
    def scalar_side_effect(*args, **kwargs):
        call = scalar_side_effect.n
        scalar_side_effect.n += 1
        if call == 0:
            return SimpleNamespace(user_id=99)
        if call == 1:
            return None
        return 0

    scalar_side_effect.n = 0
    db = MagicMock()
    db.scalar.side_effect = scalar_side_effect
    db.get.return_value = SimpleNamespace(origem="codigo", participante_id=None)

    pode, motivo, _ = avaliar_remocao(
        db, saida=_saida(), current_user=_user(id=1, role=1, username="admin")
    )
    assert pode is True
    assert motivo is None


def test_avaliar_remocao_bloqueia_status_posterior():
    db = MagicMock()
    db.scalar.side_effect = [SimpleNamespace(user_id=7)]
    db.get.return_value = SimpleNamespace(origem="codigo", participante_id=None)
    pode, motivo, _ = avaliar_remocao(
        db, saida=_saida(status="em_rota"), current_user=_user()
    )
    assert pode is False
    assert "fluxo posterior" in (motivo or "").lower() or "Em rota" in (motivo or "")


def test_avaliar_remocao_bloqueia_cobranca_fechada():
    calls = {"n": 0}

    def scalar_side_effect(*args, **kwargs):
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            return SimpleNamespace(user_id=7)
        if n == 1:
            return SimpleNamespace(fechado=True)
        return 0

    db = MagicMock()
    db.scalar.side_effect = scalar_side_effect
    db.get.return_value = SimpleNamespace(origem="codigo", participante_id=None)
    pode, motivo, _ = avaliar_remocao(db, saida=_saida(), current_user=_user())
    assert pode is False
    assert "fechada" in (motivo or "").lower()


def test_resumo_base_dia_sem_execucao(monkeypatch):
    db = MagicMock()
    base = SimpleNamespace(id_base=3, base="BASE-A")
    monkeypatch.setattr(
        "coleta_leituras_service.resolver_base", lambda *_a, **_k: base
    )
    db.scalar.return_value = None
    out = resumo_base_dia(db, sub_base="SB", base_id=3, data_operacao=date.today())
    assert out["status"] == "pendente"
    assert out["total"] == 0
    assert out["base_id"] == 3


def test_remover_leitura_idempotente_via_auditoria():
    db = MagicMock()
    audit = SimpleNamespace(
        base_id=3,
        base="BASE-A",
        data_operacao=date.today(),
        codigo="BR123",
    )
    # first scalar: audit; second path for saida returns None
    results = [audit, None]

    def scalar_side_effect(*args, **kwargs):
        if results:
            return results.pop(0)
        return {"total": 1, "shopee": 1, "mercado_livre": 0, "avulso": 0}

    db.scalar.side_effect = scalar_side_effect

    # Patch obter_totais_base_dia by making subsequent scalars return execucao-like
    # Actually remover_leitura calls obter_totais_base_dia which does another scalar
    # After audit and saida=None, it calls obter_totais_base_dia -> scalar execucao
    # Reset: audit, saida None, then execucao for totais
    execucao = SimpleNamespace(
        participantes=[SimpleNamespace(shopee=1, mercado_livre=0, avulso=0)]
    )
    db.scalar.side_effect = [audit, None, execucao]

    out = remover_leitura(
        db, sub_base="SB", current_user=_user(role=1), id_saida=10
    )
    assert out["removido"] is True
    assert out["idempotente"] is True
    assert out["codigo"] == "BR123"
    assert out["totais"]["total"] == 1


def test_remover_leitura_404_sem_auditoria():
    db = MagicMock()
    db.scalar.side_effect = [None, None]
    with pytest.raises(HTTPException) as exc:
        remover_leitura(db, sub_base="SB", current_user=_user(), id_saida=999)
    assert exc.value.status_code == 404


def test_listar_leituras_monta_pode_remover(monkeypatch):
    base = SimpleNamespace(id_base=3, base="BASE-A")
    monkeypatch.setattr(
        "coleta_leituras_service.resolver_base", lambda *_a, **_k: base
    )
    saida = _saida(id_saida=11, timestamp=datetime(2026, 8, 22, 12, 0, 0))
    db = MagicMock()
    db.scalars.return_value.all.return_value = [saida]
    monkeypatch.setattr(
        "coleta_leituras_service.avaliar_remocao",
        lambda *args, **kwargs: (True, None, 7),
    )
    monkeypatch.setattr(
        "coleta_leituras_service._dono_username",
        lambda *_a, **_k: "operador",
    )
    out = listar_leituras(
        db,
        sub_base="SB",
        current_user=_user(),
        base_id=3,
        data_operacao=date.today(),
        limit=40,
    )
    assert len(out["itens"]) == 1
    assert out["itens"][0]["pode_remover"] is True
    assert out["itens"][0]["codigo"] == "BR123"
    assert out["has_more"] is False
