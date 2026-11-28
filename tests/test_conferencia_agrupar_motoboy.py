"""Agrupamento da conferência de saída por motoboy/dia (visão do operador)."""
from __future__ import annotations

import os
import re
from datetime import date, datetime

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "pytest-secret-key")

from sqlalchemy import create_engine, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from conferencia_saida_pure import status_agrupado_conferencia
from conferencia_saida_service import (
    conferencia_por_dia_periodo,
    conferir_saida,
    listar_saidas_operacionais_sessoes_motoboy_dia,
    somar_operacional_sessoes_por_motoboy_dia,
    status_conferencia_registrada_motoboys,
)
from models import (
    ConferenciaSaida,
    Motoboy,
    Owner,
    Saida,
    SaidaHistorico,
    SaidaSessao,
    SaidaSessaoItem,
    User,
)


def test_status_agrupado_prioridade_reconferir_pendente_conferida():
    assert status_agrupado_conferencia([]) == "pendente"
    assert status_agrupado_conferencia(["conferida", "conferida"]) == "conferida"
    assert status_agrupado_conferencia(["conferida", "pendente"]) == "pendente"
    assert status_agrupado_conferencia(["conferida", "reconferir", "pendente"]) == "reconferir"
    assert status_agrupado_conferencia(["pendente", None, ""]) == "pendente"


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
    ]
    with engine.begin() as conn:
        for table in tabelas:
            ddl = str(CreateTable(table).compile(dialect=dialect))
            ddl = ddl.replace("::text", "").replace("::integer", "").replace("::boolean", "")
            ddl = "\n".join(linha for linha in ddl.splitlines() if "FOREIGN KEY" not in linha.upper())
            ddl = re.sub(
                r"(\w+)\s+BIGINT NOT NULL",
                r"\1 INTEGER PRIMARY KEY",
                ddl,
                count=1,
            )
            ddl = re.sub(r",\s*PRIMARY KEY \([^)]+\)", "", ddl)
            ddl = re.sub(r",(\s*)\)", r"\1)", ddl)
            conn.exec_driver_sql(ddl)
    return sessionmaker(bind=engine)()


def _base(db):
    db.add(
        User(
            id=1,
            password_hash="x",
            username="op",
            contato="11999999999",
            status=True,
            must_change_password=False,
        )
    )
    db.add(
        Owner(
            id_owner=1,
            email="a@b.c",
            username="base",
            valor=0,
            sub_base="base-a",
            ativo=True,
            confirmacao_saida_sessoes_habilitada=True,
            conferencia_saida_habilitada=True,
        )
    )
    db.add(
        Motoboy(
            id_motoboy=10,
            user_id=1,
            sub_base="base-a",
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


def _saida(db, codigo, servico="Shopee", status="AGUARDANDO_CONFIRMACAO_SAIDA", motoboy_id=10):
    row = Saida(
        sub_base="base-a",
        username="op",
        entregador="Ana",
        motoboy_id=motoboy_id,
        codigo=codigo,
        servico=servico,
        status=status,
        data=date(2026, 11, 24),
        timestamp=datetime(2026, 11, 24, 10, 0, 0),
    )
    db.add(row)
    db.flush()
    return row


def _sessao_com_itens(db, *, saidas, status_conf="pendente"):
    sessao = SaidaSessao(
        owner_id=1,
        sub_base="base-a",
        motoboy_id=10,
        data_ref=date(2026, 11, 24),
        estado="FINALIZADA",
        versao=1,
        aberta_por_user_id=1,
        finalizada_por_user_id=1,
        finalizada_em=datetime(2026, 11, 24, 11, 0, 0),
        total_snapshot=len(saidas),
        total_shopee_snapshot=sum(1 for s in saidas if (s.servico or "").lower() == "shopee"),
        total_ml_snapshot=sum(
            1
            for s in saidas
            if (s.servico or "").lower() in ("ml", "mercado livre", "mercado_livre")
        ),
        total_avulso_snapshot=sum(1 for s in saidas if (s.servico or "").lower() == "avulso"),
    )
    db.add(sessao)
    db.flush()
    for saida in saidas:
        db.add(
            SaidaSessaoItem(
                sessao_id=sessao.id,
                id_saida=saida.id_saida,
                codigo_snapshot=saida.codigo,
                servico_snapshot=saida.servico,
                status_snapshot=saida.status,
                incluido_por_user_id=1,
                ativo_operacional=True,
            )
        )
    conf = ConferenciaSaida(
        sessao_id=sessao.id,
        sub_base="base-a",
        owner_id=1,
        motoboy_id=10,
        data_ref=date(2026, 11, 24),
        status=status_conf,
        ultima_abertura_em=datetime(2026, 11, 24, 11, 0, 0),
        qtd_no_momento=len(saidas),
    )
    db.add(conf)
    db.flush()
    return sessao, conf


def test_soma_operacional_agrupa_sessoes_sem_duplicar_pacote():
    db = _sqlite()
    _base(db)
    a = _saida(db, "A1", "Shopee")
    b = _saida(db, "A2", "Shopee")
    c = _saida(db, "B1", "Mercado Livre")
    d = _saida(db, "C1", "Avulso")
    cancelada = _saida(db, "X1", "Shopee", status="CANCELADO")
    s1, _ = _sessao_com_itens(db, saidas=[a, b])
    s2, _ = _sessao_com_itens(db, saidas=[c])
    s3, _ = _sessao_com_itens(db, saidas=[d, cancelada])

    chave = (10, date(2026, 11, 24))
    totais = somar_operacional_sessoes_por_motoboy_dia(
        db,
        sub_base="base-a",
        grupos={chave: [int(s1.id), int(s2.id), int(s3.id)]},
    )
    shopee, ml, avulso = totais[chave]
    assert (shopee, ml, avulso) == (2, 1, 1)
    assert shopee + ml + avulso == 4

    saidas = listar_saidas_operacionais_sessoes_motoboy_dia(
        db, sub_base="base-a", sessao_ids=[int(s1.id), int(s2.id), int(s3.id)]
    )
    assert {s.codigo for s in saidas} == {"A1", "A2", "B1", "C1"}


def test_conferir_dia_fecha_todas_as_sessoes_abertas():
    db = _sqlite()
    _base(db)
    a = _saida(db, "P1")
    b = _saida(db, "P2")
    c = _saida(db, "R1")
    _sessao_com_itens(db, saidas=[a, b], status_conf="pendente")
    _sessao_com_itens(db, saidas=[c], status_conf="reconferir")

    conferir_saida(
        db,
        sub_base="base-a",
        motoboy_id=10,
        data_ref=date(2026, 11, 24),
        user_id=1,
    )
    db.flush()

    rows = list(
        db.scalars(
            select(ConferenciaSaida).where(
                ConferenciaSaida.motoboy_id == 10,
                ConferenciaSaida.data_ref == date(2026, 11, 24),
            )
        ).all()
    )
    assert len(rows) == 2
    assert all(r.status == "conferida" for r in rows)

    eventos = list(db.scalars(select(SaidaHistorico.evento)).all())
    assert eventos.count("saida_conferida") == 2
    assert eventos.count("saida_reconferida") == 1


def test_fechamento_so_conferido_quando_todas_sessoes_conferidas():
    db = _sqlite()
    _base(db)
    a = _saida(db, "F1")
    b = _saida(db, "F2")
    _sessao_com_itens(db, saidas=[a], status_conf="conferida")
    _sessao_com_itens(db, saidas=[b], status_conf="pendente")

    mapa = status_conferencia_registrada_motoboys(
        db,
        sub_base="base-a",
        motoboy_ids=[10],
        periodo_inicio=date(2026, 11, 24),
        periodo_fim=date(2026, 11, 24),
    )
    assert mapa[(10, date(2026, 11, 24))] == "pendente"

    por_dia = conferencia_por_dia_periodo(
        db,
        sub_base="base-a",
        motoboy_id=10,
        periodo_inicio=date(2026, 11, 24),
        periodo_fim=date(2026, 11, 24),
    )
    assert len(por_dia) == 1
    assert por_dia[0]["status"] == "pendente"
    assert por_dia[0]["label"] == "Não conferido"

    # Fecha a pendente e o dia passa a conferido.
    conferir_saida(
        db,
        sub_base="base-a",
        motoboy_id=10,
        data_ref=date(2026, 11, 24),
        user_id=1,
    )
    db.flush()
    mapa2 = status_conferencia_registrada_motoboys(
        db,
        sub_base="base-a",
        motoboy_ids=[10],
        periodo_inicio=date(2026, 11, 24),
        periodo_fim=date(2026, 11, 24),
    )
    assert mapa2[(10, date(2026, 11, 24))] == "conferida"


def test_listar_agrupa_por_motoboy_dia_e_detalhe_sem_sessao(monkeypatch):
    from types import SimpleNamespace

    import conferencia_saida_routes as routes

    db = _sqlite()
    _base(db)
    a = _saida(db, "L1", "Shopee")
    b = _saida(db, "L2", "Shopee")
    c = _saida(db, "L3", "Avulso")
    _sessao_com_itens(db, saidas=[a, b], status_conf="pendente")
    _sessao_com_itens(db, saidas=[c], status_conf="pendente")

    user = SimpleNamespace(
        id=1,
        role=1,
        sub_base="base-a",
        conferencia_saida_habilitada=True,
    )
    monkeypatch.setattr(routes, "owner_conferencia_habilitada", lambda *a, **k: True)

    lista = routes.listar_conferencias(
        data_inicio=date(2026, 11, 24),
        data_fim=date(2026, 11, 24),
        aba="pendente",
        motoboy_id=None,
        db=db,
        current_user=user,
    )
    assert lista.total == 1
    assert len(lista.items) == 1
    item = lista.items[0]
    assert item.motoboy_id == 10
    assert item.sessao_id is None
    assert item.status == "pendente"
    assert item.qtd_no_momento == 3

    detalhe = routes.detalhe_conferencia(
        motoboy_id=10,
        data_ref=date(2026, 11, 24),
        db=db,
        current_user=user,
    )
    assert detalhe.sessao_id is None
    assert detalhe.total == 3
    assert detalhe.operacional_total == 3
    assert detalhe.sum_shopee == 2
    assert detalhe.sum_avulso == 1
    assert detalhe.snapshot_total is None

    conferido = routes.post_conferir(
        motoboy_id=10,
        body=routes.ConferirBody(data_ref=date(2026, 11, 24)),
        db=db,
        current_user=user,
    )
    assert conferido.sessao_id is None
    assert conferido.status == "conferida"

    lista_ok = routes.listar_conferencias(
        data_inicio=date(2026, 11, 24),
        data_fim=date(2026, 11, 24),
        aba="conferida",
        motoboy_id=None,
        db=db,
        current_user=user,
    )
    assert lista_ok.total == 1
    assert lista_ok.items[0].qtd_no_momento == 3

    lista_pend = routes.listar_conferencias(
        data_inicio=date(2026, 11, 24),
        data_fim=date(2026, 11, 24),
        aba="pendente",
        motoboy_id=None,
        db=db,
        current_user=user,
    )
    assert lista_pend.total == 0


def test_status_misto_aparece_somente_na_aba_mais_aberta(monkeypatch):
    from types import SimpleNamespace

    import conferencia_saida_routes as routes

    db = _sqlite()
    _base(db)
    a = _saida(db, "M1")
    b = _saida(db, "M2")
    _sessao_com_itens(db, saidas=[a], status_conf="conferida")
    _sessao_com_itens(db, saidas=[b], status_conf="pendente")

    user = SimpleNamespace(
        id=1,
        role=1,
        sub_base="base-a",
        conferencia_saida_habilitada=True,
    )
    monkeypatch.setattr(routes, "owner_conferencia_habilitada", lambda *a, **k: True)

    pend = routes.listar_conferencias(
        data_inicio=date(2026, 11, 24),
        data_fim=date(2026, 11, 24),
        aba="pendente",
        motoboy_id=None,
        db=db,
        current_user=user,
    )
    conf = routes.listar_conferencias(
        data_inicio=date(2026, 11, 24),
        data_fim=date(2026, 11, 24),
        aba="conferida",
        motoboy_id=None,
        db=db,
        current_user=user,
    )
    assert pend.total == 1
    assert pend.items[0].qtd_no_momento == 2
    assert conf.total == 0
