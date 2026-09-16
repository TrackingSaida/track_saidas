"""Testes de envio próprio (RTE), normalizer e admissão cross-tenant."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

from types import SimpleNamespace
from unittest.mock import MagicMock

from codigo_normalizer import normalize_codigo, is_qr_like_scan_payload
from envio_proprio_service import (
    admitir_envio_proprio_no_tenant,
    gerar_codigo_rte,
    is_codigo_rte,
)
from saida_prerequisito import avaliar_prerequisito_saida


def test_is_codigo_rte_compacto():
    assert is_codigo_rte("RTE25082600001")
    assert is_codigo_rte("rte25082600001")
    assert not is_codigo_rte("RTE2508260000")  # 10 dígitos — curto demais
    assert not is_codigo_rte("AVULSO-000001")
    assert not is_codigo_rte("BR2656127018725")


def test_normalize_codigo_rte_como_avulso():
    c, servico, qr = normalize_codigo("RTE25082600001", strict_qr=True)
    assert c == "RTE25082600001"
    assert servico == "Avulso"
    assert qr is None
    assert is_qr_like_scan_payload("RTE25082600001")


def test_etiquetado_bloqueia_saida_com_coleta():
    err = avaliar_prerequisito_saida(
        coleta_habilitada=True,
        entrada_habilitada=False,
        saida_existe=True,
        status_norm="ETIQUETADO",
    )
    assert err is not None
    assert err["code"] in ("NAO_COLETADO", "PRE_REQUISITO_SAIDA", "ETIQUETADO")


def test_etiquetado_bloqueia_saida_com_entrada():
    err = avaliar_prerequisito_saida(
        coleta_habilitada=False,
        entrada_habilitada=True,
        saida_existe=True,
        status_norm="ETIQUETADO",
    )
    assert err is not None


def test_etiquetado_libera_quando_sem_prerequisito():
    assert (
        avaliar_prerequisito_saida(
            coleta_habilitada=False,
            entrada_habilitada=False,
            saida_existe=True,
            status_norm="ETIQUETADO",
        )
        is None
    )


def test_gerar_codigo_rte_usa_sequence(monkeypatch):
    db = MagicMock()
    calls = {"n": 0}

    def fake_execute(stmt):
        calls["n"] += 1
        return SimpleNamespace(scalar_one=lambda: calls["n"])

    db.execute.side_effect = fake_execute
    db.scalar.return_value = None
    codigo = gerar_codigo_rte(db)
    assert codigo.startswith("RTE")
    assert is_codigo_rte(codigo)
    assert len(codigo) >= 14  # RTE + 6 data + 5 seq


def test_admitir_sem_snapshot_retorna_none():
    db = MagicMock()
    db.scalar.side_effect = [None, None]  # local, depois envio global
    out = admitir_envio_proprio_no_tenant(
        db,
        sub_base="tenant_b",
        codigo="RTE25082600001",
        status_inicial="coletado",
    )
    assert out is None


def test_admitir_idempotente_quando_ja_existe_local():
    db = MagicMock()
    local = SimpleNamespace(id_saida=99, sub_base="tenant_b", codigo="RTE25082600001")
    db.scalar.return_value = local
    out = admitir_envio_proprio_no_tenant(
        db,
        sub_base="tenant_b",
        codigo="RTE25082600001",
        status_inicial="coletado",
    )
    assert out is local
    assert db.add.call_count == 0


def test_admitir_cria_saida_local_a_partir_do_snapshot():
    db = MagicMock()
    envio = SimpleNamespace(
        codigo="RTE25082600001",
        remetente_nome="Seller A",
        dest_nome="Cliente",
        dest_rua="Rua X",
        dest_numero="10",
        dest_complemento=None,
        dest_bairro="Centro",
        dest_cidade="São Paulo",
        dest_uf="SP",
        dest_cep="01001000",
        dest_telefone="11999999999",
    )

    # 1ª scalar: local None; 2ª: get_envio_by_codigo_global
    db.scalar.side_effect = [None, envio]

    created = []

    def add(obj):
        created.append(obj)
        if hasattr(obj, "id_saida") and getattr(obj, "id_saida", None) is None:
            obj.id_saida = 42

    db.add.side_effect = add

    out = admitir_envio_proprio_no_tenant(
        db,
        sub_base="tenant_b",
        codigo="RTE25082600001",
        status_inicial="coletado",
        username="op_b",
        base="Seller B",
        user_id=7,
    )
    assert out is not None
    assert out.sub_base == "tenant_b"
    assert out.codigo == "RTE25082600001"
    assert out.servico == "Avulso"
    assert out.status == "coletado"
    assert len(created) >= 2  # Saida + Detail (+ Historico)


def test_pdf_envio_proprio_nao_explode_sem_logo():
    from etiqueta_pdf_service import gerar_etiqueta

    owner = SimpleNamespace(
        nome_fantasia="Base Teste",
        username="base_teste",
        sub_base="base_teste",
        slogan="Slogan de teste",
        logo_object_key=None,
    )
    pdf = gerar_etiqueta(
        modo="envio_proprio",
        codigo="RTE25082600001",
        owner=owner,
        remetente={
            "nome": "Remetente Longo Nome da Empresa XYZ Ltda",
            "telefone": "11988887777",
            "cep": "01310100",
            "rua": "Avenida Paulista com nome bem extenso para forçar quebra de linha no card",
            "numero": "1000",
            "complemento": "Sala 101",
            "bairro": "Bela Vista",
            "cidade": "São Paulo",
            "uf": "SP",
        },
        destinatario={
            "nome": "Destinatário com nome também bem longo para ellipsis",
            "telefone": "21977776666",
            "cep": "20040020",
            "rua": "Rua do Destino Muito Comprida Número Extenso",
            "numero": "50",
            "complemento": None,
            "bairro": "Centro",
            "cidade": "Rio de Janeiro",
            "uf": "RJ",
        },
        peso_kg=None,
        dimensoes=None,
    )
    assert isinstance(pdf, (bytes, bytearray))
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 500
