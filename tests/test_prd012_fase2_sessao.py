"""PRD-012 Fase 2: sessão na leitura, finalização, conferência e Ação."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

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
from saida_operacional_pure import (
    FALLBACK_ACAO_HISTORICO_ANTERIOR,
    apresentar_situacao_acao,
    conta_como_saiu_para_entrega,
    evento_tecnico_nao_substitui_acao,
    rotulo_acao_evento,
    rotulo_situacao,
    status_aguardando_confirmacao_saida,
)
from saida_sessao_service import (
    MOTIVO_SEM_ITENS_VALIDOS,
    aplicar_nova_saida_mesmo_entregador,
    finalizar_sessao,
    incluir_saida_na_sessao,
    invalidar_vinculo_operacional,
    localizar_sessao_do_item,
    visoes_conferencia_sessao,
)
from saidas_listar_service import build_operacional_ctx_from_historico_rows


def test_rotulos_e_aguardando_nao_contam_como_saiu():
    assert rotulo_acao_evento("leitura_saida") == "Leu pacote para saída"
    assert rotulo_acao_evento("sessao_leitura_finalizada") == "Finalizou leitura de saída"
    assert rotulo_acao_evento("saida_confirmada") == "Confirmou início da entrega"
    assert rotulo_acao_evento("nova_saida_mesmo_entregador") == "Pacote incluído em novo lote de saída"
    assert rotulo_situacao("AGUARDANDO_CONFIRMACAO_SAIDA") == "Aguardando confirmação de saída"
    assert status_aguardando_confirmacao_saida("AGUARDANDO_CONFIRMACAO_SAIDA")
    assert conta_como_saiu_para_entrega("AGUARDANDO_CONFIRMACAO_SAIDA") is False
    assert conta_como_saiu_para_entrega("SAIU_PARA_ENTREGA") is True
    assert conta_como_saiu_para_entrega("saiu") is True
    assert evento_tecnico_nao_substitui_acao("retry_sync")
    assert evento_tecnico_nao_substitui_acao("reconciliacao_rota")
    assert rotulo_acao_evento("retry_sync") is None


def test_evento_tecnico_nao_substitui_acao_e_legado_usa_fallback():
    agora = datetime(2026, 11, 13, 9, 0, 0)

    class Hist:
        def __init__(self, evento, minutos, ident):
            self.id_saida = 7
            self.evento = evento
            self.timestamp = agora + timedelta(minutes=minutos)
            self.id = ident
            self.user_id = 3
            self.motoboy_id_anterior = None
            self.payload = None

    ctx = build_operacional_ctx_from_historico_rows(
        [7],
        [Hist("leitura_saida", 0, 1), Hist("retry_reconciliacao", 5, 2)],
    )[7]
    assert ctx.ultimo_evento == "leitura_saida"
    assert ctx.acao_label == "Leu pacote para saída"
    assert ctx.ultimo_evento_ts == agora

    item = apresentar_situacao_acao(status="SAIU_PARA_ENTREGA", acao_label=None, acao_codigo=None)
    assert item["acao"] == FALLBACK_ACAO_HISTORICO_ANTERIOR
    assert item["acao_label"] == FALLBACK_ACAO_HISTORICO_ANTERIOR
    assert item["situacao_label"] == "Saiu para entrega"
    assert item["acao_codigo"] is None


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


def _base(db, *, flag: bool):
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
            confirmacao_saida_sessoes_habilitada=flag,
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
        data=datetime.utcnow().date(),
        timestamp=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def test_releitura_devolve_sessao_e_nao_duplica_item():
    db = _sqlite()
    _base(db, flag=True)
    saida = _saida(db, "BR100")
    primeira = incluir_saida_na_sessao(
        db, saida=saida, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    versao = int(primeira.versao)
    segunda = incluir_saida_na_sessao(
        db, saida=saida, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    assert int(segunda.id) == int(primeira.id)
    assert int(segunda.versao) == versao
    itens = db.scalars(select(SaidaSessaoItem).where(SaidaSessaoItem.id_saida == saida.id_saida)).all()
    assert len(itens) == 1
    localizada = localizar_sessao_do_item(
        db, id_saida=saida.id_saida, sub_base="base-a", motoboy_id=10
    )
    assert int(localizada.id) == int(primeira.id)
    assert db.scalar(select(SaidaHistorico).where(SaidaHistorico.id_saida == saida.id_saida)) is None


def test_finalizar_congela_totais_cria_conferencia_e_nao_declara_saida():
    db = _sqlite()
    _base(db, flag=True)
    a = _saida(db, "BR1", "Shopee")
    b = _saida(db, "BR2", "Mercado Livre")
    incluir_saida_na_sessao(db, saida=a, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True)
    sessao = incluir_saida_na_sessao(
        db, saida=b, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    with pytest.raises(HTTPException) as conflito:
        finalizar_sessao(
            db,
            sub_base="base-a",
            motoboy_id=10,
            user_id=1,
            sessao_id=int(sessao.id),
            versao_esperada=int(sessao.versao) - 1,
        )
    assert conflito.value.status_code == 409
    assert conflito.value.detail["code"] == "SESSAO_VERSAO_CONFLITO"

    final = finalizar_sessao(
        db,
        sub_base="base-a",
        motoboy_id=10,
        user_id=1,
        sessao_id=int(sessao.id),
        versao_esperada=int(sessao.versao),
    )
    assert final.estado == "FINALIZADA"
    assert final.total_snapshot == 2
    assert final.total_shopee_snapshot == 1
    assert final.total_ml_snapshot == 1
    assert a.status == "AGUARDANDO_CONFIRMACAO_SAIDA"
    assert b.status == "AGUARDANDO_CONFIRMACAO_SAIDA"
    conferencia = db.scalar(select(ConferenciaSaida).where(ConferenciaSaida.sessao_id == final.id))
    assert conferencia is not None
    assert conferencia.status == "pendente"
    eventos = db.scalars(select(SaidaHistorico.evento)).all()
    assert eventos.count("sessao_leitura_finalizada") == 2

    b.servico = "Avulso"
    b.motoboy_id = None
    b.status = "CANCELADO"
    db.flush()
    visoes = visoes_conferencia_sessao(db, final)
    assert visoes["snapshot_total"] == 2
    assert visoes["snapshot_shopee"] == 1
    assert visoes["snapshot_ml"] == 1
    assert set(visoes["snapshot_ids"]) == {int(a.id_saida), int(b.id_saida)}
    assert visoes["operacional_ids"] == [int(a.id_saida)]
    assert visoes["operacional_total"] == 1
    db.refresh(conferencia)
    assert conferencia.status == "pendente"


def test_sessao_sem_itens_validos_cancela_com_ator_sistema():
    db = _sqlite()
    _base(db, flag=True)
    saida = _saida(db, "BR9")
    sessao = incluir_saida_na_sessao(
        db, saida=saida, sub_base="base-a", motoboy_id=10, user_id=4, flag_ja_validada=True
    )
    invalidar_vinculo_operacional(db, id_saida=saida.id_saida, motivo="DESATRIBUIDO", user_id=4)
    db.flush()
    db.refresh(sessao)
    assert sessao.estado == "CANCELADA"
    assert sessao.motivo_cancelamento == MOTIVO_SEM_ITENS_VALIDOS
    assert sessao.cancelada_por_user_id is None

    aberta_vazia = SaidaSessao(
        owner_id=1,
        sub_base="base-a",
        motoboy_id=10,
        data_ref=datetime.utcnow().date(),
        estado="ABERTA",
        versao=1,
        aberta_por_user_id=4,
    )
    db.add(aberta_vazia)
    db.flush()
    vazia = finalizar_sessao(db, sub_base="base-a", motoboy_id=10, user_id=4)
    assert vazia.estado == "CANCELADA"
    assert vazia.motivo_cancelamento == MOTIVO_SEM_ITENS_VALIDOS
    assert vazia.cancelada_por_user_id is None
    assert db.scalar(select(ConferenciaSaida).where(ConferenciaSaida.sessao_id == vazia.id)) is None


def test_nova_saida_com_flag_abre_vinculo_novo_e_sem_flag_mantem_legado():
    db = _sqlite()
    _base(db, flag=True)
    saida = _saida(db, "BR50", status="SAIU_PARA_ENTREGA")
    incluir_saida_na_sessao(
        db, saida=saida, sub_base="base-a", motoboy_id=10, user_id=1, flag_ja_validada=True
    )
    nova = aplicar_nova_saida_mesmo_entregador(
        db,
        saida=saida,
        sub_base="base-a",
        user_id=1,
        payload_historico="{}",
        fluxo_ativo=True,
    )
    assert saida.status == "AGUARDANDO_CONFIRMACAO_SAIDA"
    assert nova is not None
    assert nova.estado == "ABERTA"
    ativos = db.scalars(
        select(SaidaSessaoItem).where(
            SaidaSessaoItem.id_saida == saida.id_saida,
            SaidaSessaoItem.ativo_operacional.is_(True),
        )
    ).all()
    assert len(ativos) == 1
    assert int(ativos[0].sessao_id) == int(nova.id)
    evento = db.scalar(
        select(SaidaHistorico).where(SaidaHistorico.evento == "nova_saida_mesmo_entregador")
    )
    assert evento.status_novo == "AGUARDANDO_CONFIRMACAO_SAIDA"

    legado = _saida(db, "BR51", status="SAIU_PARA_ENTREGA")
    assert (
        aplicar_nova_saida_mesmo_entregador(
            db,
            saida=legado,
            sub_base="base-a",
            user_id=1,
            payload_historico="{}",
            fluxo_ativo=False,
        )
        is None
    )
    assert legado.status == "SAIU_PARA_ENTREGA"
    assert (
        localizar_sessao_do_item(db, id_saida=legado.id_saida, sub_base="base-a", motoboy_id=10)
        is None
    )
