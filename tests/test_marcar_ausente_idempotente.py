"""Ausência repetida idempotente + status legado saiu (banner preso / Registros)."""
from __future__ import annotations

import os
import re
import sys
import types

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "pytest-ausente-idempotente")

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

from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from mobile_entregas_routes import AusenteBody, marcar_ausente, marcar_entregue
from models import MotivoAusencia, Motoboy, Owner, Saida, SaidaDetail, SaidaHistorico, User


def _sqlite():
    engine = create_engine("sqlite://")
    dialect = sqlite.dialect()
    tabelas = [
        User.__table__,
        Owner.__table__,
        Motoboy.__table__,
        Saida.__table__,
        SaidaDetail.__table__,
        SaidaHistorico.__table__,
        MotivoAusencia.__table__,
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


def _base(db):
    db.add(
        User(
            id=1,
            password_hash="x",
            username="moto",
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
            confirmacao_saida_sessoes_habilitada=False,
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
    db.add(MotivoAusencia(id=1, descricao="Ninguém em casa", ativo=True))
    db.flush()


def _saida(db, codigo, status="SAIU_PARA_ENTREGA"):
    row = Saida(
        sub_base="base-a",
        username="moto",
        entregador="Ana",
        motoboy_id=10,
        codigo=codigo,
        servico="Shopee",
        status=status,
        data=datetime.utcnow().date(),
        timestamp=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def _motoboy_user():
    return type("MotoboyUser", (), {"id": 1, "role": 4, "sub_base": "base-a", "motoboy_id": 10})()


def _patch_side_effects(monkeypatch):
    monkeypatch.setattr(
        "mobile_entregas_routes.exigir_confirmacao_saidas",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "mobile_entregas_routes.validate_campos_obrigatorios_conclusao",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "mobile_entregas_routes.sync_active_route_after_delivery_update",
        lambda *args, **kwargs: {"in_active_route": False, "rota_finalizada": False},
    )
    monkeypatch.setattr(
        "saida_transferencia_service.obsolecer_pendencias_da_saida",
        lambda *args, **kwargs: None,
    )


def test_segunda_ausencia_retorna_status_finalizado_sem_novo_historico(monkeypatch):
    db = _sqlite()
    _base(db)
    saida = _saida(db, "BR-AUS-1", status="AUSENTE")
    _patch_side_effects(monkeypatch)
    user = _motoboy_user()

    with pytest.raises(HTTPException) as exc:
        marcar_ausente(int(saida.id_saida), AusenteBody(motivo_id=1), db, user, None)

    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "STATUS_FINALIZADO"
    assert detail["status_atual"] == "AUSENTE"
    eventos = db.scalars(
        select(SaidaHistorico.evento).where(SaidaHistorico.id_saida == saida.id_saida)
    ).all()
    assert eventos == []
    db.refresh(saida)
    assert saida.status == "AUSENTE"


def test_ausente_aceita_status_legado_saiu(monkeypatch):
    db = _sqlite()
    _base(db)
    saida = _saida(db, "BR-AUS-2", status="saiu")
    _patch_side_effects(monkeypatch)
    user = _motoboy_user()

    out = marcar_ausente(int(saida.id_saida), AusenteBody(motivo_id=1), db, user, None)

    assert out["ok"] is True
    db.refresh(saida)
    assert saida.status == "AUSENTE"
    eventos = db.scalars(
        select(func.count()).select_from(SaidaHistorico).where(
            SaidaHistorico.id_saida == saida.id_saida,
            SaidaHistorico.evento == "ausente",
        )
    ).one()
    assert int(eventos) == 1


def test_ausente_rejeita_aguardando_confirmacao(monkeypatch):
    db = _sqlite()
    _base(db)
    saida = _saida(db, "BR-AUS-3", status="AGUARDANDO_CONFIRMACAO_SAIDA")
    _patch_side_effects(monkeypatch)
    user = _motoboy_user()

    with pytest.raises(HTTPException) as exc:
        marcar_ausente(int(saida.id_saida), AusenteBody(motivo_id=1), db, user, None)

    assert exc.value.status_code == 422
    assert exc.value.detail == "Status atual não permite marcar como ausente."
    db.refresh(saida)
    assert saida.status == "AGUARDANDO_CONFIRMACAO_SAIDA"


def test_entregue_aceita_status_legado_saiu(monkeypatch):
    db = _sqlite()
    _base(db)
    saida = _saida(db, "BR-ENT-1", status="saiu")
    _patch_side_effects(monkeypatch)
    user = _motoboy_user()

    out = marcar_entregue(int(saida.id_saida), None, db, user, None)

    assert out["ok"] is True
    db.refresh(saida)
    assert saida.status == "ENTREGUE"
    eventos = db.scalars(
        select(func.count()).select_from(SaidaHistorico).where(
            SaidaHistorico.id_saida == saida.id_saida,
            SaidaHistorico.evento == "entregue",
        )
    ).one()
    assert int(eventos) == 1
