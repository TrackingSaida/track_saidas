"""PRD-013: transferência pendente quando pacote está em rota ativa."""
from __future__ import annotations

import os
import re
import sys
import types
from datetime import datetime
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-prd013")

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

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from active_route_sync import pacote_em_rota_ativa
from mobile_entregas_routes import (
    RotasIniciarBody,
    _hoje_operacional,
    _upsert_rota_preparando,
    assumir_entrega,
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
    SaidaTransferenciaPendente,
    User,
)
from saida_transferencia_service import (
    CODE_AGUARDANDO,
    CODE_JA_RESOLVIDA,
    STATUS_ACEITA,
    STATUS_CANCELADA,
    STATUS_OBSOLETA,
    STATUS_PENDENTE,
    STATUS_RECUSADA,
    aceitar_transferencia,
    cancelar_transferencia,
    obsolecer_pendencias_da_saida,
    pendencia_ativa_da_saida,
    recusar_transferencia,
    solicitar_reatribuicao,
    tentar_gate_reatribuicao,
)


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
        SaidaTransferenciaPendente.__table__,
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


def _pessoa(db, user_id, username, motoboy_id, sub_base):
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
                confirmacao_saida_sessoes_habilitada=False,
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


def _base(db):
    _pessoa(db, 1, "op", 10, "base-a")
    if db.get(Motoboy, 11) is None:
        _pessoa(db, 2, "op2", 11, "base-a")


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


def _motoboy_user(motoboy_id=10, user_id=1):
    return type(
        "MotoboyUser",
        (),
        {
            "id": user_id,
            "role": 4,
            "sub_base": "base-a",
            "motoboy_id": motoboy_id,
            "conferencia_saida_habilitada": True,
        },
    )()


def _staff_user(user_id=99):
    return type(
        "StaffUser",
        (),
        {"id": user_id, "role": 1, "sub_base": "base-a", "motoboy_id": None},
    )()


def _rota_ativa(db, motoboy_id, ordem, *, parada=0):
    rota = _upsert_rota_preparando(
        db,
        motoboy_id=motoboy_id,
        sub_base="base-a",
        ref_date=_hoje_operacional(),
        ordem=ordem,
        polyline_encoded="abc",
        geometry_provider="osrm",
    )
    rotas_iniciar(RotasIniciarBody(ordem=ordem), db, _motoboy_user(motoboy_id))
    db.refresh(rota)
    rota.parada_atual = parada
    db.commit()
    db.refresh(rota)
    return rota


@patch("saida_transferencia_service._push_origem")
@patch("saida_transferencia_service._push_resolucao")
def test_gate_cria_pendencia_sem_trocar_posse(mock_res, mock_orig):
    db = _sqlite()
    _base(db)
    a = _saida(db, "T1")
    b = _saida(db, "T2")
    _rota_ativa(db, 10, [int(a.id_saida), int(b.id_saida)])

    assert pacote_em_rota_ativa(db, motoboy_id=10, id_saida=int(a.id_saida), sub_base="base-a")

    with pytest.raises(HTTPException) as err:
        tentar_gate_reatribuicao(
            db,
            saida=a,
            sub_base="base-a",
            motoboy_destino_id=11,
            solicitante_user_id=99,
        )
    assert err.value.status_code == 409
    assert err.value.detail["code"] == CODE_AGUARDANDO
    assert "motoboy_origem_nome" in err.value.detail
    assert "motoboy_destino_nome" in err.value.detail
    assert err.value.detail["motoboy_origem_nome"]
    assert err.value.detail["motoboy_destino_nome"]
    assert "está com" in str(err.value.detail["message"]).lower()
    db.refresh(a)
    assert a.motoboy_id == 10
    pend = pendencia_ativa_da_saida(db, int(a.id_saida))
    assert pend is not None
    assert pend.status == STATUS_PENDENTE
    assert int(pend.motoboy_destino_id) == 11


@patch("saida_transferencia_service._push_origem")
@patch("saida_transferencia_service._push_resolucao")
@patch("push_notification_service.enqueue_pacotes_atribuidos")
def test_aceitar_remove_da_rota_e_troca_titular(mock_enq, mock_res, mock_orig):
    db = _sqlite()
    _base(db)
    a = _saida(db, "T3")
    b = _saida(db, "T4")
    rota = _rota_ativa(db, 10, [int(a.id_saida), int(b.id_saida)])

    with pytest.raises(HTTPException):
        solicitar_reatribuicao(
            db,
            saida=a,
            sub_base="base-a",
            motoboy_destino_id=11,
            solicitante_user_id=99,
            solicitante_motoboy_id=11,
        )
    pend = pendencia_ativa_da_saida(db, int(a.id_saida))
    assert pend is not None

    result = aceitar_transferencia(
        db,
        transferencia_id=int(pend.id),
        motoboy_id=10,
        sub_base="base-a",
        user_id=1,
    )
    assert result["status"] == STATUS_ACEITA
    db.refresh(a)
    db.refresh(rota)
    assert a.motoboy_id == 11
    ordem = (
        __import__("json").loads(rota.ordem_json)
        if isinstance(rota.ordem_json, str)
        else rota.ordem_json
    )
    assert int(a.id_saida) not in [int(x) for x in ordem]
    assert int(b.id_saida) in [int(x) for x in ordem]


@patch("saida_transferencia_service._push_origem")
@patch("saida_transferencia_service._push_resolucao")
def test_recusar_mantem_posse_e_rota(mock_res, mock_orig):
    db = _sqlite()
    _base(db)
    a = _saida(db, "T5")
    b = _saida(db, "T6")
    rota = _rota_ativa(db, 10, [int(a.id_saida), int(b.id_saida)])
    with pytest.raises(HTTPException):
        solicitar_reatribuicao(
            db, saida=a, sub_base="base-a", motoboy_destino_id=11, solicitante_user_id=99
        )
    pend = pendencia_ativa_da_saida(db, int(a.id_saida))
    recusar_transferencia(
        db,
        transferencia_id=int(pend.id),
        motoboy_id=10,
        sub_base="base-a",
        user_id=1,
    )
    db.refresh(a)
    db.refresh(rota)
    assert a.motoboy_id == 10
    ordem = (
        __import__("json").loads(rota.ordem_json)
        if isinstance(rota.ordem_json, str)
        else rota.ordem_json
    )
    assert int(a.id_saida) in [int(x) for x in ordem]
    assert pendencia_ativa_da_saida(db, int(a.id_saida)) is None
    db.refresh(pend)
    assert pend.status == STATUS_RECUSADA


@patch("saida_transferencia_service._push_origem")
@patch("saida_transferencia_service._push_resolucao")
def test_cancelar_por_staff(mock_res, mock_orig):
    db = _sqlite()
    _base(db)
    a = _saida(db, "T7")
    b = _saida(db, "T8")
    _rota_ativa(db, 10, [int(a.id_saida), int(b.id_saida)])
    with pytest.raises(HTTPException):
        solicitar_reatribuicao(
            db, saida=a, sub_base="base-a", motoboy_destino_id=11, solicitante_user_id=99
        )
    pend = pendencia_ativa_da_saida(db, int(a.id_saida))
    cancelar_transferencia(
        db,
        transferencia_id=int(pend.id),
        sub_base="base-a",
        user=_staff_user(),
        is_staff=True,
    )
    db.refresh(pend)
    assert pend.status == STATUS_CANCELADA
    assert a.motoboy_id == 10


@patch("saida_transferencia_service._push_origem")
@patch("saida_transferencia_service._push_resolucao")
def test_substituir_pendencia(mock_res, mock_orig):
    db = _sqlite()
    _base(db)
    a = _saida(db, "T9")
    b = _saida(db, "T10")
    _rota_ativa(db, 10, [int(a.id_saida), int(b.id_saida)])
    with pytest.raises(HTTPException):
        solicitar_reatribuicao(
            db, saida=a, sub_base="base-a", motoboy_destino_id=11, solicitante_user_id=2
        )
    pend1 = pendencia_ativa_da_saida(db, int(a.id_saida))
    tid = int(pend1.id)
    with pytest.raises(HTTPException):
        solicitar_reatribuicao(
            db, saida=a, sub_base="base-a", motoboy_destino_id=11, solicitante_user_id=99
        )
    pend2 = pendencia_ativa_da_saida(db, int(a.id_saida))
    assert int(pend2.id) == tid
    assert int(pend2.solicitante_user_id) == 99


@patch("saida_transferencia_service._push_origem")
@patch("saida_transferencia_service._push_resolucao")
def test_obsolecer_na_baixa(mock_res, mock_orig):
    db = _sqlite()
    _base(db)
    a = _saida(db, "T11")
    b = _saida(db, "T12")
    _rota_ativa(db, 10, [int(a.id_saida), int(b.id_saida)])
    with pytest.raises(HTTPException):
        solicitar_reatribuicao(
            db, saida=a, sub_base="base-a", motoboy_destino_id=11, solicitante_user_id=99
        )
    n = obsolecer_pendencias_da_saida(db, id_saida=int(a.id_saida), user_id=1)
    db.commit()
    assert n == 1
    assert pendencia_ativa_da_saida(db, int(a.id_saida)) is None


@patch("saida_transferencia_service._push_origem")
@patch("saida_transferencia_service._push_resolucao")
def test_preparando_sem_gate(mock_res, mock_orig):
    db = _sqlite()
    _base(db)
    a = _saida(db, "T13")
    b = _saida(db, "T14")
    _upsert_rota_preparando(
        db,
        motoboy_id=10,
        sub_base="base-a",
        ref_date=_hoje_operacional(),
        ordem=[int(a.id_saida), int(b.id_saida)],
    )
    gated = tentar_gate_reatribuicao(
        db,
        saida=a,
        sub_base="base-a",
        motoboy_destino_id=11,
        solicitante_user_id=99,
    )
    assert gated is False
    assert pendencia_ativa_da_saida(db, int(a.id_saida)) is None


@patch("saida_transferencia_service._push_origem")
@patch("saida_transferencia_service._push_resolucao")
def test_assumir_com_rota_ativa_aguarda(mock_res, mock_orig):
    db = _sqlite()
    _base(db)
    a = _saida(db, "T15")
    b = _saida(db, "T16")
    _rota_ativa(db, 10, [int(a.id_saida), int(b.id_saida)])
    with pytest.raises(HTTPException) as err:
        assumir_entrega(int(a.id_saida), db, _motoboy_user(11, user_id=2))
    assert err.value.status_code == 409
    assert err.value.detail["code"] == CODE_AGUARDANDO
    db.refresh(a)
    assert a.motoboy_id == 10


@patch("saida_transferencia_service._push_origem")
@patch("saida_transferencia_service._push_resolucao")
def test_segundo_aceite_ja_resolvida(mock_res, mock_orig):
    db = _sqlite()
    _base(db)
    a = _saida(db, "T17")
    b = _saida(db, "T18")
    _rota_ativa(db, 10, [int(a.id_saida), int(b.id_saida)])
    with pytest.raises(HTTPException):
        solicitar_reatribuicao(
            db, saida=a, sub_base="base-a", motoboy_destino_id=11, solicitante_user_id=99
        )
    pend = pendencia_ativa_da_saida(db, int(a.id_saida))
    recusar_transferencia(
        db, transferencia_id=int(pend.id), motoboy_id=10, sub_base="base-a", user_id=1
    )
    with pytest.raises(HTTPException) as err:
        aceitar_transferencia(
            db,
            transferencia_id=int(pend.id),
            motoboy_id=10,
            sub_base="base-a",
            user_id=1,
        )
    assert err.value.detail["code"] == CODE_JA_RESOLVIDA


@patch("saida_transferencia_service._push_origem")
@patch("saida_transferencia_service._push_resolucao")
def test_otimizar_persiste_sufixo_rota_ativa(mock_res, mock_orig):
    db = _sqlite()
    _base(db)
    a = _saida(db, "T19")
    b = _saida(db, "T20")
    c = _saida(db, "T21")
    ordem = [int(a.id_saida), int(b.id_saida), int(c.id_saida)]
    rota = _rota_ativa(db, 10, ordem, parada=1)
    nova = [int(a.id_saida), int(c.id_saida), int(b.id_saida)]
    updated = _upsert_rota_preparando(
        db,
        motoboy_id=10,
        sub_base="base-a",
        ref_date=_hoje_operacional(),
        ordem=nova,
        polyline_encoded="xyz",
        geometry_provider="google",
    )
    db.refresh(updated)
    assert updated.status == "ativa"
    got = (
        __import__("json").loads(updated.ordem_json)
        if isinstance(updated.ordem_json, str)
        else updated.ordem_json
    )
    assert [int(x) for x in got] == nova
    assert updated.geometry_status == "valid"
