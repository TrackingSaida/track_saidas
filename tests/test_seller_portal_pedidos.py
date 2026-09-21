"""Testes do acompanhamento de pedidos no portal do seller."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from codigo_normalizer import canonicalize_servico
from seller_portal_pedidos_service import (
    canal_venda_chave,
    canal_venda_label,
    rotulo_timeline_seller,
    saida_visivel_ao_seller,
    status_pedido_amigavel,
    tipo_timeline_seller,
)


def test_canal_avulso_vira_site():
    assert canonicalize_servico("Avulso") == "Avulso"
    assert canal_venda_chave("Avulso") == "site"
    assert canal_venda_label("avulso") == "Site"
    assert canal_venda_label(None) == "Site"


def test_canal_marketplaces():
    assert canal_venda_label("shopee") == "Shopee"
    assert canal_venda_label("Mercado Livre") == "Mercado Livre"
    assert canal_venda_label("ml") == "Mercado Livre"
    assert canal_venda_chave("Shopee") == "shopee"


def test_status_amigavel_seller():
    assert status_pedido_amigavel("ETIQUETADO") == "Aguardando coleta"
    assert status_pedido_amigavel("COLETADO") == "Coletado"
    assert status_pedido_amigavel("EM_ROTA") == "Saiu para entrega"
    assert status_pedido_amigavel("SAIU_PARA_ENTREGA") == "Saiu para entrega"
    assert status_pedido_amigavel("AUSENTE") == "Destinatário ausente"
    assert status_pedido_amigavel("ENTREGUE") == "Entregue"
    assert status_pedido_amigavel("CANCELADO") == "Cancelado"


def test_timeline_oculta_eventos_internos():
    assert rotulo_timeline_seller("scan") is None
    assert rotulo_timeline_seller("lido") is None
    assert rotulo_timeline_seller("saida_conferida") is None
    assert rotulo_timeline_seller("etiqueta_gerada") == "Etiqueta emitida"
    assert rotulo_timeline_seller("criado_coleta") == "Pacote coletado"
    assert rotulo_timeline_seller("em_rota") == "Saiu para entrega"
    assert rotulo_timeline_seller("entregue") == "Entregue"
    assert tipo_timeline_seller("ausente") == "ausente"


def _seller(**kwargs):
    defaults = {"sub_base": "BASE_A", "id_base": 10}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_visivel_via_envio_proprio_mesmo_com_base_alterada():
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(sub_base="BASE_A", id_base=10)
    saida = SimpleNamespace(id_saida=1, sub_base="BASE_A", base="Outra Loja")
    assert saida_visivel_ao_seller(db, _seller(), saida) is True


def test_nao_visivel_envio_de_outro_seller():
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(sub_base="BASE_A", id_base=99)
    saida = SimpleNamespace(id_saida=1, sub_base="BASE_A", base="Loja X")
    assert saida_visivel_ao_seller(db, _seller(), saida) is False


def test_visivel_coleta_marketplace_pelo_nome_da_loja():
    db = MagicMock()
    db.scalar.return_value = None
    db.get.return_value = SimpleNamespace(base="Loja X", sub_base="BASE_A")
    saida = SimpleNamespace(id_saida=7, sub_base="BASE_A", base="Loja X")
    assert saida_visivel_ao_seller(db, _seller(), saida) is True


def test_nao_visivel_outra_loja_sem_envio():
    db = MagicMock()
    db.scalar.return_value = None
    db.get.return_value = SimpleNamespace(base="Loja X", sub_base="BASE_A")
    saida = SimpleNamespace(id_saida=7, sub_base="BASE_A", base="Loja Y")
    assert saida_visivel_ao_seller(db, _seller(), saida) is False


def test_nao_visivel_outro_tenant():
    db = MagicMock()
    saida = SimpleNamespace(id_saida=7, sub_base="OUTRA", base="Loja X")
    assert saida_visivel_ao_seller(db, _seller(), saida) is False


def test_timeline_projeta_somente_eventos_do_seller(monkeypatch):
    from seller_portal_pedidos_service import projetar_timeline_seller
    from saida_historico_service import SaidaHistoricoItemOut

    ts = datetime(2026, 9, 19, 10, 0, 0)
    monkeypatch.setattr(
        "seller_portal_pedidos_service.listar_historico_saida",
        lambda db, id_saida: [
            SaidaHistoricoItemOut(id=1, id_saida=9, evento="scan", timestamp=ts, acao_label="Escaneou"),
            SaidaHistoricoItemOut(
                id=2,
                id_saida=9,
                evento="criado_coleta",
                timestamp=ts,
                acao_label="Pacote coletado",
            ),
            SaidaHistoricoItemOut(
                id=3,
                id_saida=9,
                evento="ausente",
                timestamp=ts,
                motivo_ocorrencia="Não atendeu",
                tentativa=1,
            ),
        ],
    )
    out = projetar_timeline_seller(MagicMock(), 9)
    # Mais recente no topo (padrão de tracking).
    assert [x["titulo"] for x in out] == ["Destinatário ausente", "Pacote coletado"]
    assert out[0]["detalhe"] == "Tentativa 1: Não atendeu"
    assert "scan" not in str(out)


def test_detalhe_404_quando_nao_visivel(monkeypatch):
    from seller_portal_pedidos_service import detalhe_pedido_seller

    db = MagicMock()
    db.get.return_value = SimpleNamespace(id_saida=1, sub_base="OUTRA", base="X")
    with pytest.raises(HTTPException) as exc:
        detalhe_pedido_seller(db, _seller(), 1)
    assert exc.value.status_code == 404
    assert "não encontrado" in str(exc.value.detail).lower()
