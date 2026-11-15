"""PRD-012 Fase 4: ciclo da rota, reatribuição e ação obsoleta."""
from __future__ import annotations

import sys
import types

try:
    import boto3  # noqa: F401
except ImportError:
    _boto3 = types.ModuleType("boto3")
    _botocore = types.ModuleType("botocore")
    _client = types.ModuleType("botocore.client")

    class _Config:
        def __init__(self, *args, **kwargs):
            pass

    _client.Config = _Config
    sys.modules["boto3"] = _boto3
    sys.modules["botocore"] = _botocore
    sys.modules["botocore.client"] = _client

import re
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from active_route_sync import MOTIVO_ROTA_SEM_ITENS, encerrar_item_da_operacao_anterior
from mobile_entregas_routes import (
    FinalizarLoteBody,
    IniciarRotaBody,
    RotasIniciarBody,
    RotasOrdemBody,
    _get_saida_for_motoboy,
    _hoje_operacional,
    _upsert_rota_preparando,
    finalizar_lote,
    iniciar_rota,
    rotas_atualizar_ordem,
    rotas_iniciar,
)
from models import (
    ConferenciaSaida,
    Motoboy,
    Owner,
    RotasMotoboy,
    Saida,
    SaidaConfirmacaoIdempotencia,
    SaidaHistorico,
    SaidaSessao,
    SaidaSessaoItem,
    User,
)
from route_api_status import API_STATUS_ROTA_PRONTA, map_rota_to_api_status
from saida_sessao_service import confirmar_sessoes_motoboy, finalizar_sessao, incluir_saida_na_sessao


def _sqlite():
    engine = create_engine("sqlite://")
    dialect = sqlite.dialect()
    tabelas = [
        User.__table__,
        Owner.__table__,
        Motoboy.__table__,
        Saida.__table__,
        SaidaHistorico.__table__,
        SaidaSessao.__table__,
        SaidaSessaoItem.__table__,
        ConferenciaSaida.__table__,
        RotasMotoboy.__table__,
        SaidaConfirmacaoIdempotencia.__table__,
    ]
    with engine.begin() as conn:
        for table in tabelas:
            ddl = str(CreateTable(table).compile(dialect=dialect))
            ddl = ddl.replace("::text", "").replace("::integer", "").replace("::boolean", "")
            ddl = "\n".join(linha for linha in ddl.splitlines() if "FOREIGN KEY" not in linha.upper())
            ddl = re.sub(r"(\w+)\s+BIGINT NOT NULL", r"\1 INTEGER PRIMARY KEY", ddl, count=1)
            ddl = re.sub(r",\s*PRIMARY KEY \([^)]+\)", "", ddl)
            ddl = re.sub(r",(\s*)\)", r"\1)", ddl)
            conn.exec_driver_sql(ddl)
    return sessionmaker(bind=engine)()


def _pessoa(db, user_id, username, motoboy_id, sub_base, *, flag):
    db.add(
        User(
            id=user_id,
            password_hash="x",
            username=username,
            contato=f"1199999999{user_id}",
            status=True,
            must_change_password=False,
        )
    )
    if db.get(Owner, 1) is None and sub_base == "base-a":
        db.add(
            Owner(
                id_owner=1,
                email="a@b.c",
                username="base",
                valor=0,
                sub_base="base-a",
                ativo=True,
                confirmacao_saida_sessoes_habilitada=flag,
            )
        )
    db.add(
        Motoboy(
            id_motoboy=motoboy_id,
            user_id=user_id,
            sub_base=sub_base,
            rua="Rua",
            numero="1",
            bairro="Centro",
            cidade="Sao Paulo",
            cep="01001000",
            pode_ler_coleta=False,
            pode_realizar_coleta=False,
            pode_ler_saida=True,
            pode_digitar_codigo_manual=True,
            pode_lancar_avulso=True,
            pode_criar_avulso_coleta=True,
            pode_criar_avulso_saida=False,
            avulso_exige_foto=False,
            claims_version=1,
        )
    )
    db.flush()


def _base(db, *, flag: bool):
    _pessoa(db, 1, "op", 10, "base-a", flag=flag)
    if db.get(Motoboy, 11) is None:
        _pessoa(db, 2, "op2", 11, "base-a", flag=flag)


def _saida(db, codigo, status="SAIU_PARA_ENTREGA", motoboy_id=10):
    row = Saida(
        sub_base="base-a",
        username="op",
        entregador="Ana",
        motoboy_id=motoboy_id,
        codigo=codigo,
        servico="Shopee",
        status=status,
        data=_hoje_operacional(),
        timestamp=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def _motoboy(motoboy_id=10):
    return type(
        "MotoboyUser",
        (),
        {"id": 1, "role": 4, "sub_base": "base-a", "motoboy_id": motoboy_id, "conferencia_saida_habilitada": True},
    )()


def _eventos(db, id_saida):
    return list(
        db.scalars(select(SaidaHistorico.evento).where(SaidaHistorico.id_saida == id_saida)).all()
    )


def test_confirmar_saida_grava_saiu_sem_em_rota_nem_rota_ativa():
    db = _sqlite()
    _base(db, flag=True)
    saida = _saida(db, "BR1", status="AGUARDANDO_CONFIRMACAO_SAIDA")
    sessao = incluir_saida_na_sessao(
        db, saida=saida, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    final = finalizar_sessao(db, sub_base="base-a", motoboy_id=10, user_id=1, sessao_id=int(sessao.id))
    confirmar_sessoes_motoboy(
        db,
        sub_base="base-a",
        motoboy_id=10,
        sessao_ids=[int(final.id)],
        ids_removidos=[],
        user_id=1,
        chave_idempotente="confirma-1",
    )
    db.commit()
    db.refresh(saida)
    assert saida.status == "SAIU_PARA_ENTREGA"
    assert "saida_confirmada" in _eventos(db, saida.id_saida)
    assert "em_rota" not in _eventos(db, saida.id_saida)
    assert "rota_iniciada" not in _eventos(db, saida.id_saida)
    assert db.scalar(select(RotasMotoboy).where(RotasMotoboy.status == "ativa")) is None


def test_otimizacao_fica_pronta_e_so_iniciar_ativa():
    db = _sqlite()
    _base(db, flag=True)
    a = _saida(db, "BR2")
    b = _saida(db, "BR3")
    hoje = _hoje_operacional()
    rota = _upsert_rota_preparando(
        db,
        motoboy_id=10,
        sub_base="base-a",
        ref_date=hoje,
        ordem=[int(a.id_saida), int(b.id_saida)],
    )
    db.refresh(a)
    db.refresh(b)
    assert rota.status == "preparando"
    assert rota.iniciado_em is None
    assert map_rota_to_api_status(rota) == API_STATUS_ROTA_PRONTA
    assert a.status == "SAIU_PARA_ENTREGA"
    assert b.status == "SAIU_PARA_ENTREGA"

    ordem = [int(b.id_saida), int(a.id_saida)]
    revisao = int(rota.route_revision or 0)
    rotas_atualizar_ordem(int(rota.id), RotasOrdemBody(ordem=ordem), db, _motoboy())
    db.refresh(rota)
    db.refresh(a)
    assert rota.status == "preparando"
    assert rota.iniciado_em is None
    assert int(rota.route_revision or 0) == revisao + 1
    assert a.status == "SAIU_PARA_ENTREGA"
    assert map_rota_to_api_status(rota) == API_STATUS_ROTA_PRONTA

    resposta = rotas_iniciar(RotasIniciarBody(ordem=ordem), db, _motoboy())
    db.refresh(rota)
    db.refresh(a)
    db.refresh(b)
    assert resposta.rota_id == str(rota.id)
    assert rota.status == "ativa"
    assert rota.iniciado_em is not None
    assert a.status == "EM_ROTA"
    assert b.status == "EM_ROTA"
    assert "rota_iniciada" in _eventos(db, a.id_saida)
    assert "em_rota" not in _eventos(db, a.id_saida)


def test_retirada_ajusta_rota_e_nao_insere_no_motoboy_novo():
    db = _sqlite()
    _base(db, flag=True)
    a = _saida(db, "BR4")
    b = _saida(db, "BR5")
    c = _saida(db, "BR6")
    do_novo = _saida(db, "BR7", motoboy_id=11)
    hoje = _hoje_operacional()
    rota = _upsert_rota_preparando(
        db,
        motoboy_id=10,
        sub_base="base-a",
        ref_date=hoje,
        ordem=[int(a.id_saida), int(b.id_saida), int(c.id_saida)],
        polyline_encoded="abc",
        geometry_provider="osrm",
    )
    rota.parada_atual = 2
    db.commit()
    rota_nova = _upsert_rota_preparando(
        db,
        motoboy_id=11,
        sub_base="base-a",
        ref_date=hoje,
        ordem=[int(do_novo.id_saida)],
    )
    sessao = incluir_saida_na_sessao(
        db, saida=a, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    incluir_saida_na_sessao(
        db, saida=b, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    conferencia = ConferenciaSaida(
        sessao_id=int(sessao.id),
        sub_base="base-a",
        motoboy_id=10,
        data_ref=hoje,
        status="conferida",
    )
    db.add(conferencia)
    db.commit()
    revisao = int(rota.route_revision or 0)

    resultado = encerrar_item_da_operacao_anterior(
        db,
        sub_base="base-a",
        motoboy_id=10,
        id_saida=int(a.id_saida),
        motivo="REATRIBUIDO",
        user_id=1,
    )
    db.commit()
    db.refresh(rota)
    db.refresh(rota_nova)
    db.refresh(conferencia)
    item = db.scalar(select(SaidaSessaoItem).where(SaidaSessaoItem.id_saida == a.id_saida))
    assert item.ativo_operacional is False
    assert item.motivo_remocao == "REATRIBUIDO"
    assert conferencia.status == "conferida"
    ordem = [int(x) for x in __import__("json").loads(rota.ordem_json)]
    assert ordem == [int(b.id_saida), int(c.id_saida)]
    assert int(rota.parada_atual) == 1
    assert int(rota.route_revision or 0) == revisao + 1
    assert rota.geometry_status == "stale"
    assert rota.geometry_order_hash is None
    assert rota.status == "preparando"
    assert resultado["inserido_em_outra_rota"] is False
    assert [int(x) for x in __import__("json").loads(rota_nova.ordem_json)] == [int(do_novo.id_saida)]
    assert rota_nova.status == "preparando"


def test_rota_ativa_vazia_finaliza_e_preparando_vazia_cancela():
    db = _sqlite()
    _base(db, flag=True)
    ativa_item = _saida(db, "BR8")
    pronta_item = _saida(db, "BR9")
    hoje = _hoje_operacional()
    preparando = _upsert_rota_preparando(
        db,
        motoboy_id=10,
        sub_base="base-a",
        ref_date=hoje,
        ordem=[int(ativa_item.id_saida)],
    )
    rotas_iniciar(RotasIniciarBody(ordem=[int(ativa_item.id_saida)]), db, _motoboy())
    db.refresh(preparando)
    encerrar_item_da_operacao_anterior(
        db,
        sub_base="base-a",
        motoboy_id=10,
        id_saida=int(ativa_item.id_saida),
        motivo="CANCELADO",
        user_id=1,
    )
    db.commit()
    db.refresh(preparando)
    assert preparando.status == "finalizada"
    assert preparando.motivo_encerramento == MOTIVO_ROTA_SEM_ITENS
    assert preparando.finalizado_em is not None

    outra = _upsert_rota_preparando(
        db,
        motoboy_id=11,
        sub_base="base-a",
        ref_date=hoje,
        ordem=[int(pronta_item.id_saida)],
    )
    encerrar_item_da_operacao_anterior(
        db,
        sub_base="base-a",
        motoboy_id=11,
        id_saida=int(pronta_item.id_saida),
        motivo="DESATRIBUIDO",
        user_id=1,
    )
    db.commit()
    db.refresh(outra)
    assert outra.status == "cancelada"
    assert outra.motivo_encerramento == MOTIVO_ROTA_SEM_ITENS


def test_acao_obsoleta_409_e_lote_misto_descarta_so_o_obsoleto(monkeypatch):
    db = _sqlite()
    _base(db, flag=True)
    valido = _saida(db, "BR10")
    reatribuido = _saida(db, "BR11", motoboy_id=11)
    cancelado = _saida(db, "BR12", status="CANCELADO")
    solto = _saida(db, "BR13", motoboy_id=None)

    with pytest.raises(HTTPException) as erro_reat:
        _get_saida_for_motoboy(db, int(reatribuido.id_saida), 10, "base-a")
    assert erro_reat.value.status_code == 409
    assert erro_reat.value.detail["code"] == "ACAO_PEDIDO_OBSOLETA"
    assert erro_reat.value.detail["reason"] == "REATRIBUIDO"
    assert "motoboy" not in erro_reat.value.detail["message"].lower() or "removido" in erro_reat.value.detail["message"]

    with pytest.raises(HTTPException) as erro_cancel:
        _get_saida_for_motoboy(db, int(cancelado.id_saida), 10, "base-a")
    assert erro_cancel.value.detail["reason"] == "CANCELADO"

    with pytest.raises(HTTPException) as erro_solto:
        _get_saida_for_motoboy(db, int(solto.id_saida), 10, "base-a")
    assert erro_solto.value.detail["reason"] == "DESATRIBUIDO"

    monkeypatch.setattr("mobile_entregas_routes.get_active_route_delivery_ids", lambda *args, **kwargs: [])
    monkeypatch.setattr("mobile_entregas_routes._carregar_details_por_saida_ids", lambda *args, **kwargs: {})
    monkeypatch.setattr("mobile_entregas_routes._get_detail_for_saida", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "mobile_entregas_routes.validate_campos_obrigatorios_conclusao",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "mobile_entregas_routes.sync_active_route_after_delivery_update",
        lambda *args, **kwargs: {"in_active_route": False, "rota_finalizada": False},
    )
    resposta = finalizar_lote(
        FinalizarLoteBody(ids=[int(valido.id_saida), int(reatribuido.id_saida)], acao="entregue"),
        db,
        _motoboy(),
    )
    db.refresh(valido)
    db.refresh(reatribuido)
    assert valido.status == "ENTREGUE"
    assert reatribuido.status == "SAIU_PARA_ENTREGA"
    assert [item.id_saida for item in resposta.finalizados] == [int(valido.id_saida)]
    assert [(item.id_saida, item.reason) for item in resposta.obsoletos] == [
        (int(reatribuido.id_saida), "REATRIBUIDO")
    ]
    assert "11" not in resposta.obsoletos[0].message


def test_flag_desligada_preserva_legado():
    db = _sqlite()
    _base(db, flag=False)
    saida = _saida(db, "BR14")
    iniciar_rota(IniciarRotaBody(delivery_ids=[int(saida.id_saida)]), db, _motoboy())
    db.refresh(saida)
    assert saida.status == "EM_ROTA"
    assert "em_rota" in _eventos(db, saida.id_saida)
    assert "rota_iniciada" not in _eventos(db, saida.id_saida)

    outro = _saida(db, "BR15")
    hoje = _hoje_operacional()
    rota = _upsert_rota_preparando(
        db,
        motoboy_id=10,
        sub_base="base-a",
        ref_date=hoje,
        ordem=[int(outro.id_saida)],
    )
    rotas_iniciar(RotasIniciarBody(ordem=[int(outro.id_saida)]), db, _motoboy())
    db.refresh(rota)
    db.refresh(outro)
    assert rota.status == "ativa"
    assert "em_rota" in _eventos(db, outro.id_saida)
    assert "rota_iniciada" not in _eventos(db, outro.id_saida)


def test_flag_ligada_iniciar_rota_legado_nao_grava_em_rota():
    db = _sqlite()
    _base(db, flag=True)
    saida = _saida(db, "BR16")
    with pytest.raises(HTTPException) as erro:
        iniciar_rota(IniciarRotaBody(delivery_ids=[int(saida.id_saida)]), db, _motoboy())
    assert erro.value.status_code == 409
    assert erro.value.detail["code"] == "USE_CONFIRMAR_SAIDA"
    db.refresh(saida)
    assert saida.status == "SAIU_PARA_ENTREGA"
