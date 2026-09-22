"""Testes de transferência de base de coleta operacional."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from coleta_leituras_service import (
    _aplicar_delta_servico,
    _quantidade_participante,
    transferir_base_coleta,
)


def test_aplicar_delta_servico_incrementa_e_decrementa():
    part = SimpleNamespace(
        shopee=2, mercado_livre=1, avulso=0, pacotes_g=1, g_shopee=1, g_ml=0, g_avulso=0
    )
    _aplicar_delta_servico(part, servico_key="shopee", is_grande=True, delta=-1)
    assert part.shopee == 1
    assert part.g_shopee == 0
    assert part.pacotes_g == 0
    _aplicar_delta_servico(part, servico_key="mercado_livre", is_grande=False, delta=1)
    assert part.mercado_livre == 2
    assert _quantidade_participante(part) == 3


def _user(**overrides):
    values = {"id": 7, "role": 2, "username": "operador", "sub_base": "SB"}
    values.update(overrides)
    return SimpleNamespace(**values)


def _saida(**overrides):
    values = {
        "id_saida": 10,
        "id_coleta": 5,
        "status": "saiu",
        "motoboy_id": 3,
        "data": date.today(),
        "timestamp": datetime.now(),
        "username": "operador",
        "codigo": "BR123",
        "servico": "Shopee",
        "base": "FABFLAY CONFERENCIA",
        "sub_base": "SB",
        "is_grande": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_transferir_exige_mesma_base_origem():
    db = MagicMock()
    s1 = _saida(id_saida=1, base="A")
    s2 = _saida(id_saida=2, base="B")
    db.scalars.return_value.all.return_value = [s1, s2]

    with pytest.raises(HTTPException) as exc:
        transferir_base_coleta(
            db,
            sub_base="SB",
            current_user=_user(),
            ids_saida=[1, 2],
            base_destino="C",
        )
    assert exc.value.status_code == 422
    assert "mesma base" in str(exc.value.detail).lower()


def test_transferir_exige_mesmo_dia():
    db = MagicMock()
    s1 = _saida(id_saida=1, data=date.today())
    s2 = _saida(id_saida=2, data=date.today() - timedelta(days=1))
    db.scalars.return_value.all.return_value = [s1, s2]

    with pytest.raises(HTTPException) as exc:
        transferir_base_coleta(
            db,
            sub_base="SB",
            current_user=_user(),
            ids_saida=[1, 2],
            base_destino="FABFLAY LOGISTICA",
        )
    assert exc.value.status_code == 422
    assert "mesmo dia" in str(exc.value.detail).lower()


def test_transferir_bloqueia_destino_igual_origem():
    db = MagicMock()
    db.scalars.return_value.all.return_value = [_saida()]

    with pytest.raises(HTTPException) as exc:
        transferir_base_coleta(
            db,
            sub_base="SB",
            current_user=_user(),
            ids_saida=[10],
            base_destino="FABFLAY CONFERENCIA",
        )
    assert exc.value.status_code == 422
    assert "diferente" in str(exc.value.detail).lower()


def test_transferir_exige_coleta_vinculada():
    db = MagicMock()
    db.scalars.return_value.all.return_value = [_saida(id_coleta=None)]

    with pytest.raises(HTTPException) as exc:
        transferir_base_coleta(
            db,
            sub_base="SB",
            current_user=_user(),
            ids_saida=[10],
            base_destino="FABFLAY LOGISTICA",
        )
    assert exc.value.status_code == 422
    assert "coleta" in str(exc.value.detail).lower()


def test_transferir_fluxo_feliz_usa_core_update_e_ledger():
    hoje = date.today()
    saida = _saida(id_saida=10, data=hoje, servico="Mercado Livre")
    coleta_origem = SimpleNamespace(
        id_coleta=5,
        sub_base="SB",
        base="FABFLAY CONFERENCIA",
        participante_id=11,
        execucao_id=21,
        shopee=0,
        mercado_livre=1,
        avulso=0,
        pacotes_g=0,
        g_shopee=0,
        g_ml=0,
        g_avulso=0,
        valor_total=0,
    )
    part_origem = SimpleNamespace(
        id_participante=11,
        execucao_id=21,
        shopee=0,
        mercado_livre=1,
        avulso=0,
        pacotes_g=0,
        g_shopee=0,
        g_ml=0,
        g_avulso=0,
        sem_volume=False,
        versao=1,
        status="finalizado",
        atualizado_em=None,
        atualizado_por_user_id=7,
    )
    exec_origem = SimpleNamespace(id_execucao=21, status="coletado", participantes=[part_origem])
    base_origem = SimpleNamespace(id_base=1, base="FABFLAY CONFERENCIA", ativo=True)
    base_dest = SimpleNamespace(id_base=2, base="FABFLAY LOGISTICA", ativo=True)
    exec_dest = SimpleNamespace(id_execucao=22, status="em_coleta", modo="codigo", participantes=[])
    part_dest = SimpleNamespace(
        id_participante=12,
        execucao_id=22,
        user_id=7,
        shopee=0,
        mercado_livre=0,
        avulso=0,
        pacotes_g=0,
        g_shopee=0,
        g_ml=0,
        g_avulso=0,
        sem_volume=False,
        versao=1,
        status="finalizado",
        atualizado_em=None,
        atualizado_por_user_id=7,
    )

    db = MagicMock()
    db.scalars.return_value.all.return_value = [saida]
    gets = {
        5: coleta_origem,
        11: part_origem,
        21: exec_origem,
        22: exec_dest,
    }
    db.get.side_effect = lambda model, ident: gets.get(ident)

    with patch("coleta_leituras_service.resolver_base", side_effect=[base_dest, base_origem]), \
         patch("coleta_leituras_service._garantir_nao_fechado"), \
         patch("coleta_leituras_service.obter_ou_criar_execucao", return_value=exec_dest), \
         patch(
             "coleta_leituras_service._obter_ou_criar_participante_destino",
             return_value=part_dest,
         ), \
         patch("coleta_leituras_service._recalcular_coleta_sem_commit", return_value=coleta_origem), \
         patch("coleta_leituras_service._limpar_execucao_sem_volume"), \
         patch("coleta_leituras_service.atualizar_status_execucao"), \
         patch(
             "coleta_leituras_service.obter_totais_base_dia",
             side_effect=[
                 {"total": 0, "shopee": 0, "mercado_livre": 0, "avulso": 0},
                 {"total": 1, "shopee": 0, "mercado_livre": 1, "avulso": 0},
             ],
         ), \
         patch("coleta_leituras_service.invalidate_listar_cache"), \
         patch("coleta_leituras_service.resolver_executor", return_value=(_user(), None)):
        def add_side_effect(obj):
            if getattr(obj, "base", None) == "FABFLAY LOGISTICA" and not hasattr(obj, "id_saida"):
                obj.id_coleta = 99
                gets[99] = obj

        db.add.side_effect = add_side_effect

        result = transferir_base_coleta(
            db,
            sub_base="SB",
            current_user=_user(),
            ids_saida=[10],
            base_destino="FABFLAY LOGISTICA",
        )

    assert result["transferidos"] == 1
    assert result["base_origem"] == "FABFLAY CONFERENCIA"
    assert result["base_destino"] == "FABFLAY LOGISTICA"
    assert result["status_origem"] == "pendente"
    assert result["status_destino"] == "coletado"
    assert result["contagem"]["mercado_livre"] == 1
    assert part_origem.mercado_livre == 0
    assert part_dest.mercado_livre == 1
    # Sem atribuição ORM em Saida (evita saida_after_update / commit aninhado).
    assert saida.base == "FABFLAY CONFERENCIA"
    assert db.execute.called
    stmt = db.execute.call_args[0][0]
    assert "Update" in type(stmt).__name__ or "UPDATE" in type(stmt).__name__.upper()
    db.expunge.assert_called()
    db.commit.assert_called_once()


def test_obter_ou_criar_participante_destino_cria_com_em_coleta():
    """Volume 0 + finalizado viola ck_coleta_participante_volume; criar em_coleta."""
    from coleta_leituras_service import _obter_ou_criar_participante_destino

    db = MagicMock()
    db.scalar.return_value = None
    execucao = SimpleNamespace(id_execucao=22)
    added = []

    def capture_add(obj):
        added.append(obj)

    db.add.side_effect = capture_add

    with patch(
        "coleta_leituras_service.resolver_executor",
        return_value=(_user(id=7, username="operador"), None),
    ):
        part = _obter_ou_criar_participante_destino(
            db,
            execucao=execucao,
            sub_base="SB",
            current_user=_user(),
        )

    assert len(added) == 1
    assert added[0] is part
    assert part.status == "em_coleta"
    assert part.shopee == 0
    assert part.mercado_livre == 0
    assert part.avulso == 0
    db.flush.assert_called()


def test_recalcular_coleta_vazia_nullifica_saidas_antes_de_deletar():
    from coleta_leituras_service import _recalcular_coleta_sem_commit

    db = MagicMock()
    db.scalars.return_value.all.return_value = []
    coleta = SimpleNamespace(
        id_coleta=5, execucao_id=21, participante_id=11
    )

    result = _recalcular_coleta_sem_commit(db, coleta)

    assert result is None
    assert db.execute.called
    stmt = db.execute.call_args[0][0]
    assert "Update" in type(stmt).__name__ or "UPDATE" in type(stmt).__name__.upper()
    assert coleta.execucao_id is None
    assert coleta.participante_id is None
    db.delete.assert_called_once_with(coleta)
    db.flush.assert_called()


def test_limpar_execucao_nullifica_coleta_antes_de_deletar_participante_zerado():
    from coleta_leituras_service import _limpar_execucao_sem_volume

    part_zero = SimpleNamespace(
        id_participante=11,
        execucao_id=21,
        shopee=0,
        mercado_livre=0,
        avulso=0,
        pacotes_g=0,
        g_shopee=0,
        g_ml=0,
        g_avulso=0,
    )
    coleta_vinculada = SimpleNamespace(
        id_coleta=5, participante_id=11, execucao_id=21
    )
    execucao = SimpleNamespace(id_execucao=21, status="coletado")

    db = MagicMock()
    # 1ª chamada: vivos; 2ª: coletas do participante; 3ª: restantes; 4ª: coletas da execução
    scalar_lists = [
        [part_zero],
        [coleta_vinculada],
        [],
        [],
    ]

    def scalars_side_effect(*_a, **_k):
        result = MagicMock()
        result.all.return_value = scalar_lists.pop(0) if scalar_lists else []
        return result

    db.scalars.side_effect = scalars_side_effect
    db.get.return_value = execucao

    with patch("coleta_leituras_service.atualizar_status_execucao"):
        _limpar_execucao_sem_volume(db, execucao)

    assert coleta_vinculada.participante_id is None
    assert coleta_vinculada.execucao_id is None
    db.delete.assert_any_call(part_zero)
    db.delete.assert_any_call(execucao)


def test_transferir_fluxo_cria_participante_destino_sem_mock():
    """Transferência sem mock de _obter_ou_criar_participante_destino (caminho CHECK)."""
    hoje = date.today()
    saida = _saida(id_saida=10, data=hoje, servico="Shopee")
    coleta_origem = SimpleNamespace(
        id_coleta=5,
        sub_base="SB",
        base="FABFLAY CONFERENCIA",
        participante_id=11,
        execucao_id=21,
        shopee=1,
        mercado_livre=0,
        avulso=0,
        pacotes_g=0,
        g_shopee=0,
        g_ml=0,
        g_avulso=0,
        valor_total=0,
    )
    part_origem = SimpleNamespace(
        id_participante=11,
        execucao_id=21,
        shopee=1,
        mercado_livre=0,
        avulso=0,
        pacotes_g=0,
        g_shopee=0,
        g_ml=0,
        g_avulso=0,
        sem_volume=False,
        versao=1,
        status="finalizado",
        atualizado_em=None,
        atualizado_por_user_id=7,
    )
    exec_origem = SimpleNamespace(id_execucao=21, status="coletado", participantes=[part_origem])
    base_origem = SimpleNamespace(id_base=1, base="FABFLAY CONFERENCIA", ativo=True)
    base_dest = SimpleNamespace(id_base=2, base="FABFLAY LOGISTICA", ativo=True)
    exec_dest = SimpleNamespace(id_execucao=22, status="em_coleta", modo="codigo", participantes=[])

    db = MagicMock()
    db.scalars.return_value.all.return_value = [saida]
    # Sem participante no destino → _obter_ou_criar cria novo.
    db.scalar.return_value = None
    gets = {
        5: coleta_origem,
        11: part_origem,
        21: exec_origem,
        22: exec_dest,
    }
    db.get.side_effect = lambda model, ident: gets.get(ident)

    created_parts = []

    def add_side_effect(obj):
        if getattr(obj, "base", None) == "FABFLAY LOGISTICA" and not hasattr(obj, "id_saida"):
            obj.id_coleta = 99
            gets[99] = obj
        if hasattr(obj, "status") and hasattr(obj, "user_id") and not hasattr(obj, "id_saida"):
            if not hasattr(obj, "id_participante"):
                obj.id_participante = 12
            created_parts.append(obj)
            gets[12] = obj

    db.add.side_effect = add_side_effect

    with patch("coleta_leituras_service.resolver_base", side_effect=[base_dest, base_origem]), \
         patch("coleta_leituras_service._garantir_nao_fechado"), \
         patch("coleta_leituras_service.obter_ou_criar_execucao", return_value=exec_dest), \
         patch("coleta_leituras_service._recalcular_coleta_sem_commit", return_value=None), \
         patch("coleta_leituras_service._limpar_execucao_sem_volume"), \
         patch("coleta_leituras_service.atualizar_status_execucao"), \
         patch(
             "coleta_leituras_service.obter_totais_base_dia",
             side_effect=[
                 {"total": 0, "shopee": 0, "mercado_livre": 0, "avulso": 0},
                 {"total": 1, "shopee": 1, "mercado_livre": 0, "avulso": 0},
             ],
         ), \
         patch("coleta_leituras_service.invalidate_listar_cache"), \
         patch("coleta_leituras_service.resolver_executor", return_value=(_user(), None)):
        result = transferir_base_coleta(
            db,
            sub_base="SB",
            current_user=_user(),
            ids_saida=[10],
            base_destino="FABFLAY LOGISTICA",
        )

    assert result["transferidos"] == 1
    assert created_parts, "deve criar participante destino"
    assert created_parts[0].status == "finalizado"  # incrementado após create em_coleta
    assert created_parts[0].shopee == 1
    assert part_origem.shopee == 0
    db.commit.assert_called_once()
