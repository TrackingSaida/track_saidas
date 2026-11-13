"""PRD-012 Fase 3: contratos de sessão, idempotência e gates."""
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
from datetime import date, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from conferencia_saida_routes import (
    ConfirmarLeituraBody,
    confirmar_leitura,
    detalhe_conferencia_sessao,
    recuperar_sessao_aberta_por_query,
)
from mobile_entregas_routes import (
    AusenteBody,
    ConfirmarSaidaSessoesBody,
    FinalizarLoteBody,
    _scan_session_payload,
    _upsert_rota_preparando,
    confirmar_saida_sessoes,
    finalizar_lote,
    listar_saida_sessoes_pendentes,
    marcar_ausente,
    marcar_entregue,
)
from models import (
    ConferenciaSaida,
    Motoboy,
    Owner,
    Saida,
    SaidaConfirmacaoIdempotencia,
    SaidaHistorico,
    SaidaSessao,
    SaidaSessaoItem,
    User,
)
from saida_sessao_service import (
    buscar_confirmacao_idempotente,
    campos_sessao_leitura,
    carregar_sessao_no_escopo,
    confirmar_sessoes_motoboy,
    exigir_confirmacao_saidas,
    finalizar_sessao,
    incluir_saida_na_sessao,
    recuperar_sessao_aberta_staff,
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


def _saida(db, codigo, status="AGUARDANDO_CONFIRMACAO_SAIDA", motoboy_id=10, sub_base="base-a"):
    row = Saida(
        sub_base=sub_base,
        username="op",
        entregador="Ana",
        motoboy_id=motoboy_id,
        codigo=codigo,
        servico="Shopee",
        status=status,
        data=datetime.utcnow().date(),
        timestamp=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def _staff(sub_base="base-a"):
    return type(
        "Staff",
        (),
        {"id": 1, "role": 1, "sub_base": sub_base, "conferencia_saida_habilitada": True},
    )()


def _motoboy_user(motoboy_id=10, sub_base="base-a"):
    return type("MotoboyUser", (), {"id": 1, "role": 4, "sub_base": sub_base, "motoboy_id": motoboy_id})()


def _detail(exc):
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["code"] == "SAIDA_NAO_CONFIRMADA"
    assert detail["quantidade"] >= 1
    assert isinstance(detail["sessao_ids"], list)
    return detail


def test_leitura_devolve_sessao_id_status_e_total_vivo():
    db = _sqlite()
    _base(db, flag=True)
    a = _saida(db, "BR1")
    b = _saida(db, "BR2")
    sessao = incluir_saida_na_sessao(
        db, saida=a, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    sessao = incluir_saida_na_sessao(
        db, saida=b, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    campos = campos_sessao_leitura(db, sessao)
    assert campos["sessao_id"] == int(sessao.id)
    assert campos["sessao_status"] == "ABERTA"
    assert campos["sessao_total"] == 2
    payload = _scan_session_payload(sessao, ativo=True, db=db)
    assert payload["sessao_id"] == campos["sessao_id"]
    assert payload["sessao_status"] == "ABERTA"
    assert payload["sessao_total"] == 2


def test_confirmar_leitura_finaliza_e_nao_declara_saida():
    db = _sqlite()
    _base(db, flag=True)
    saida = _saida(db, "BR10")
    sessao = incluir_saida_na_sessao(
        db, saida=saida, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    out = confirmar_leitura(
        ConfirmarLeituraBody(
            motoboy_id=10,
            data_ref=date.today(),
            sessao_id=int(sessao.id),
            versao=int(sessao.versao),
        ),
        db,
        _staff(),
    )
    assert out.sessao_id == int(sessao.id)
    assert out.sessao_status == "FINALIZADA"
    db.refresh(saida)
    assert saida.status == "AGUARDANDO_CONFIRMACAO_SAIDA"
    assert db.scalar(select(func.count()).select_from(SaidaSessao)) == 1


def test_confirmacao_idempotente_nao_duplica_status_sessao_nem_historico():
    db = _sqlite()
    _base(db, flag=True)
    saida = _saida(db, "BR20")
    incluir_saida_na_sessao(
        db, saida=saida, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    body = ConfirmarSaidaSessoesBody(chave_idempotente="tentativa-42")
    user = _motoboy_user()
    primeiro = confirmar_saida_sessoes(body, db, user)
    sessoes = db.scalar(select(func.count()).select_from(SaidaSessao))
    historicos = db.scalar(select(func.count()).select_from(SaidaHistorico))
    db.refresh(saida)
    assert saida.status == "SAIU_PARA_ENTREGA"
    assert primeiro["total_confirmado"] == 1
    assert buscar_confirmacao_idempotente(
        db, sub_base="base-a", motoboy_id=10, chave_idempotente="tentativa-42"
    )

    segundo = confirmar_saida_sessoes(body, db, user)
    db.refresh(saida)
    assert segundo["idempotente"] is True
    assert segundo["ids_confirmados"] == primeiro["ids_confirmados"]
    assert saida.status == "SAIU_PARA_ENTREGA"
    assert db.scalar(select(func.count()).select_from(SaidaSessao)) == sessoes
    assert db.scalar(select(func.count()).select_from(SaidaHistorico)) == historicos
    assert db.scalar(select(func.count()).select_from(SaidaConfirmacaoIdempotencia)) == 1


def test_confirmacao_ignora_item_que_deixou_de_ser_elegivel():
    db = _sqlite()
    _base(db, flag=True)
    elegivel = _saida(db, "BR30")
    cancelado = _saida(db, "BR31")
    incluir_saida_na_sessao(
        db, saida=elegivel, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    sessao = incluir_saida_na_sessao(
        db, saida=cancelado, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    final = finalizar_sessao(db, sub_base="base-a", motoboy_id=10, user_id=1, sessao_id=int(sessao.id))
    cancelado.status = "CANCELADO"
    db.flush()
    resultado = confirmar_sessoes_motoboy(
        db,
        sub_base="base-a",
        motoboy_id=10,
        sessao_ids=[int(final.id)],
        ids_removidos=[],
        user_id=1,
        chave_idempotente="elegiveis",
    )
    db.refresh(elegivel)
    db.refresh(cancelado)
    assert elegivel.status == "SAIU_PARA_ENTREGA"
    assert cancelado.status == "CANCELADO"
    assert int(cancelado.id_saida) in resultado["ids_ignorados"]
    assert int(cancelado.id_saida) not in resultado["ids_confirmados"]
    eventos = db.scalars(
        select(SaidaHistorico.evento).where(SaidaHistorico.id_saida == cancelado.id_saida)
    ).all()
    assert "saida_confirmada" not in eventos


def test_gates_rejeitam_nao_confirmado_com_sessoes_e_quantidade():
    db = _sqlite()
    _base(db, flag=True)
    pendente = _saida(db, "BR40")
    confirmado = _saida(db, "BR41", status="SAIU_PARA_ENTREGA")
    sessao = incluir_saida_na_sessao(
        db, saida=pendente, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    user = _motoboy_user()
    ids = [int(confirmado.id_saida), int(pendente.id_saida)]

    with pytest.raises(HTTPException) as entrega:
        marcar_entregue(int(pendente.id_saida), None, db, user, None)
    detalhe_entrega = _detail(entrega)
    assert detalhe_entrega["quantidade"] == 1
    assert int(sessao.id) in detalhe_entrega["sessao_ids"]

    with pytest.raises(HTTPException) as ausencia:
        marcar_ausente(int(pendente.id_saida), AusenteBody(motivo_id=1), db, user, None)
    assert _detail(ausencia)["quantidade"] == 1

    with pytest.raises(HTTPException) as lote:
        finalizar_lote(FinalizarLoteBody(ids=ids, acao="entregue"), db, user)
    assert _detail(lote)["quantidade"] == 1

    with pytest.raises(HTTPException) as preparacao:
        _upsert_rota_preparando(
            db,
            motoboy_id=10,
            sub_base="base-a",
            ref_date=date.today(),
            ordem=ids,
        )
    assert _detail(preparacao)["quantidade"] == 1

    with pytest.raises(HTTPException) as otimizacao:
        exigir_confirmacao_saidas(db, sub_base="base-a", saida_ids=ids)
    assert _detail(otimizacao)["quantidade"] == 1
    assert int(sessao.id) in _detail(otimizacao)["sessao_ids"]

    db.refresh(pendente)
    db.refresh(confirmado)
    assert pendente.status == "AGUARDANDO_CONFIRMACAO_SAIDA"
    assert confirmado.status == "SAIU_PARA_ENTREGA"


def test_flag_desligada_nao_aplica_gate(monkeypatch):
    db = _sqlite()
    _base(db, flag=False)
    saida = _saida(db, "BR50", status="AGUARDANDO_CONFIRMACAO_SAIDA")
    assert (
        exigir_confirmacao_saidas(db, sub_base="base-a", saida_ids=[int(saida.id_saida)])
        is None
    )
    monkeypatch.setattr(
        "mobile_entregas_routes.get_active_route_delivery_ids", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        "mobile_entregas_routes._carregar_details_por_saida_ids", lambda *args, **kwargs: {}
    )
    resposta = finalizar_lote(
        FinalizarLoteBody(ids=[int(saida.id_saida)], acao="entregue"),
        db,
        _motoboy_user(),
    )
    db.refresh(saida)
    assert resposta.finalizados == []
    assert saida.status == "AGUARDANDO_CONFIRMACAO_SAIDA"


def test_isolamento_de_sessao_aberta_e_conferencia():
    db = _sqlite()
    _base(db, flag=True)
    db.add(
        Owner(
            id_owner=2,
            email="b@b.c",
            username="base-b",
            valor=0,
            sub_base="base-b",
            ativo=True,
            confirmacao_saida_sessoes_habilitada=True,
        )
    )
    _pessoa(db, 2, "op-b", 20, "base-b", flag=True)
    _pessoa(db, 3, "op-a2", 11, "base-a", flag=True)
    minha = incluir_saida_na_sessao(
        db,
        saida=_saida(db, "BR60"),
        sub_base="base-a",
        motoboy_id=10,
        user_id=1,
        flag_ja_validada=True,
    )
    outra = incluir_saida_na_sessao(
        db,
        saida=_saida(db, "BR61", motoboy_id=11),
        sub_base="base-a",
        motoboy_id=11,
        user_id=1,
        flag_ja_validada=True,
    )
    alheia = incluir_saida_na_sessao(
        db,
        saida=_saida(db, "BR62", motoboy_id=20, sub_base="base-b"),
        sub_base="base-b",
        motoboy_id=20,
        user_id=2,
        flag_ja_validada=True,
    )
    vista = recuperar_sessao_aberta_staff(db, sub_base="base-a", motoboy_id=10)
    assert vista["sessao_id"] == int(minha.id)
    via_alias = recuperar_sessao_aberta_por_query(10, db, _staff())
    assert via_alias.sessao_id == int(minha.id)
    assert via_alias.sessao_total == 1
    with pytest.raises(HTTPException) as outro_motoboy_de_outra_base:
        recuperar_sessao_aberta_staff(db, sub_base="base-a", motoboy_id=20)
    assert outro_motoboy_de_outra_base.value.status_code == 404
    do_colega = recuperar_sessao_aberta_staff(db, sub_base="base-a", motoboy_id=11)
    assert do_colega["sessao_id"] == int(outra.id)
    assert do_colega["sessao_id"] != int(minha.id)

    minha.owner_id = 99
    db.flush()
    with pytest.raises(HTTPException) as owner_errado:
        carregar_sessao_no_escopo(db, sessao_id=int(minha.id), sub_base="base-a")
    assert owner_errado.value.status_code == 404
    with pytest.raises(HTTPException) as detalhe:
        detalhe_conferencia_sessao(int(alheia.id), db, _staff("base-a"))
    assert detalhe.value.status_code == 404

    pendentes = listar_saida_sessoes_pendentes(db, _motoboy_user(10, "base-a"))
    assert pendentes["quantidade"] == len(pendentes["itens"])
    assert pendentes["itens"]
    assert {int(item["sessao_id"]) for item in pendentes["itens"]} == {int(minha.id)}
    assert int(alheia.id) not in {int(item["sessao_id"]) for item in pendentes["itens"]}
