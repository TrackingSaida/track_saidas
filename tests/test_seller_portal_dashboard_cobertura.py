"""Testes de cobertura CEP com regiões e dashboard seller."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from cobertura_cep_service import (
    avaliar_cobertura_detalhada,
    cep_coberto,
    listar_cobertura_estruturada,
    normalize_prefixo,
)
from seller_portal_pedidos_service import status_pedido_amigavel


def test_status_unificado_em_rota():
    assert status_pedido_amigavel("EM_ROTA") == "Saiu para entrega"
    assert status_pedido_amigavel("ETIQUETADO") == "Aguardando coleta"
    assert status_pedido_amigavel("CANCELADO") == "Cancelado"


def test_cep_coberto_lista_vazia_permite():
    assert cep_coberto("06414000", []) is True
    assert cep_coberto("06414000", ["064"]) is True
    assert cep_coberto("07000000", ["064"]) is False


def test_normalize_prefixo():
    assert normalize_prefixo("064-") == "064"
    assert normalize_prefixo(" 06 4 ") == "064"


def test_listar_cobertura_estruturada_ilimitado():
    db = MagicMock()
    db.scalars.return_value.all.side_effect = [[], []]
    out = listar_cobertura_estruturada(db, "BASE_A")
    assert out["modo"] == "ilimitado"
    assert out["regioes"] == []
    assert out["prefixos_sem_regiao"] == []


def test_avaliar_cobertura_detalhada_com_regiao(monkeypatch):
    estrutura = {
        "modo": "regioes",
        "regioes": [{"id": 1, "nome": "Barueri", "prefixos": ["064"]}],
        "prefixos_sem_regiao": [],
    }
    monkeypatch.setattr(
        "cobertura_cep_service.listar_cobertura_estruturada",
        lambda db, sub: estrutura,
    )
    monkeypatch.setattr(
        "cobertura_cep_service.list_prefixos_ativos",
        lambda db, sub: ["064"],
    )
    out = avaliar_cobertura_detalhada(MagicMock(), "BASE_A", "06414-000")
    assert out["coberto"] is True
    assert out["regiao_nome"] == "Barueri"
    assert out["modo"] == "regioes"
    assert out["prefixo_match"] == "064"


def test_dashboard_parse_periodo_hoje():
    from seller_portal_dashboard_service import _parse_periodo

    start, end, label = _parse_periodo("hoje", None, None)
    assert label == "hoje"
    assert end > start
    assert (end - start).days == 1


def test_dashboard_kpis_escopo(monkeypatch):
    from seller_portal_dashboard_service import dashboard_seller

    seller = SimpleNamespace(sub_base="BASE_A", id_base=10)
    agora = datetime.utcnow()
    saida = SimpleNamespace(
        id_saida=1,
        status="AUSENTE",
        servico="Avulso",
        timestamp=agora,
        codigo="RTE1",
        ml_order_id=None,
    )

    db = MagicMock()
    monkeypatch.setattr(
        "seller_portal_dashboard_service._nome_base_seller",
        lambda db, s: "Loja X",
    )
    monkeypatch.setattr(
        "seller_portal_dashboard_service.filtro_pedidos_seller",
        lambda s, n: True,
    )
    db.scalars.return_value.all.return_value = [saida]
    monkeypatch.setattr(
        "seller_portal_dashboard_service._load_envios_map",
        lambda db, ids: {},
    )
    monkeypatch.setattr(
        "seller_portal_dashboard_service._load_details_map",
        lambda db, ids: {},
    )

    out = dashboard_seller(db, seller, periodo="hoje")
    assert out["kpis"]["recebidos"] == 1
    assert out["kpis"]["ausentes"] == 1
    assert out["kpis"]["atencao"] == 1
    assert out["atencao"]["filtro_status"] == "ausente"
