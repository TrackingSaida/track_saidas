"""PRD-011 Fatias 1 e 2: papel do cliente, preço a pagar da base e cálculo do repasse."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from base_papel_pure import (
    eh_base_parceira,
    eh_seller,
    exige_papel_obrigatorio,
    normalizar_papel,
    permite_autenticacao_portal,
)
from base_repasse_pure import (
    calcular_repasse,
    excecoes_preco_pagar,
    normalizar_preco_pagar,
    origem_precos_pagar,
    periodos_sobrepostos,
    resolver_precos_pagar,
    status_elegivel_pagamento,
    status_permite_reajuste,
    totais_canais,
    validar_destino_repasse,
    valor_final_repasse,
)

GLOBAL = {"shopee_valor": Decimal("5.00"), "ml_valor": Decimal("6.50"), "avulso_valor": Decimal("4.00")}


# ---- papel ---------------------------------------------------------------
def test_normalizar_papel_aceita_valores_validos_e_vazio():
    assert normalizar_papel(" Seller ") == "seller"
    assert normalizar_papel("BASE_PARCEIRA") == "base_parceira"
    assert normalizar_papel(None) is None
    assert normalizar_papel("  ") is None


def test_normalizar_papel_recusa_valor_desconhecido():
    with pytest.raises(ValueError):
        normalizar_papel("subbase")


def test_papel_obrigatorio_so_para_owner_base():
    assert exige_papel_obrigatorio("base") is True
    assert exige_papel_obrigatorio("subbase") is False
    assert exige_papel_obrigatorio(None) is False


def test_portal_so_para_seller_no_owner_base():
    assert permite_autenticacao_portal("seller", "base") is True
    assert permite_autenticacao_portal("base_parceira", "base") is False
    assert permite_autenticacao_portal(None, "subbase") is True
    assert eh_seller("seller") and not eh_seller("base_parceira")
    assert eh_base_parceira("base_parceira") and not eh_base_parceira(None)


def test_destino_repasse_exige_base_parceira_que_entrega_e_ativa():
    assert validar_destino_repasse(papel="base_parceira", entrega_pedidos=True, ativo=True) is None
    assert validar_destino_repasse(papel="seller", entrega_pedidos=True, ativo=True)
    assert validar_destino_repasse(papel="base_parceira", entrega_pedidos=False, ativo=True)
    assert validar_destino_repasse(papel="base_parceira", entrega_pedidos=True, ativo=False)


# ---- preço a pagar -------------------------------------------------------
def test_preco_igual_ao_global_ou_vazio_nao_grava_excecao():
    exc = excecoes_preco_pagar(
        {"shopee_valor": "5.00", "ml_valor": None, "avulso_valor": Decimal("4")}, GLOBAL
    )
    assert exc == {"shopee_valor": None, "ml_valor": None, "avulso_valor": None}


def test_so_o_canal_diferente_do_global_vira_excecao():
    exc = excecoes_preco_pagar({"shopee_valor": "5.00", "ml_valor": "7.25", "avulso_valor": None}, GLOBAL)
    assert exc == {"shopee_valor": None, "ml_valor": Decimal("7.25"), "avulso_valor": None}


def test_preco_usa_duas_casas_e_rejeita_negativo():
    assert normalizar_preco_pagar("7,255") == Decimal("7.26")
    assert normalizar_preco_pagar("") is None
    with pytest.raises(ValueError):
        normalizar_preco_pagar("-1")
    with pytest.raises(ValueError):
        normalizar_preco_pagar("abc")


def test_efetivo_herda_global_e_marca_origem_por_canal():
    excecao = {"shopee_valor": None, "ml_valor": Decimal("7.25"), "avulso_valor": None}
    efetivo = resolver_precos_pagar(GLOBAL, excecao)
    assert efetivo == {
        "shopee_valor": Decimal("5.00"),
        "ml_valor": Decimal("7.25"),
        "avulso_valor": Decimal("4.00"),
    }
    assert origem_precos_pagar(excecao) == {
        "shopee_valor": "padrao",
        "ml_valor": "proprio",
        "avulso_valor": "padrao",
    }
    # Global muda depois: o canal sem exceção acompanha.
    novo_global = {**GLOBAL, "shopee_valor": Decimal("5.50")}
    assert resolver_precos_pagar(novo_global, excecao)["shopee_valor"] == Decimal("5.50")


# ---- cálculo do repasse --------------------------------------------------
def test_calcular_repasse_agrupa_por_dia_e_zera_cancelado():
    precos = resolver_precos_pagar(GLOBAL, None)
    d1, d2 = date(2026, 11, 2), date(2026, 11, 3)
    itens, total = calcular_repasse(
        [
            (d2, "Mercado Livre", "enviado_para_base"),
            (d1, "Shopee", "ENVIADO_PARA_BASE"),
            (d1, "Shopee", "cancelado"),
            (d1, "Avulso", "enviado_para_base"),
        ],
        precos,
    )
    assert [i["data"] for i in itens] == [d1, d2]
    assert itens[0] == {
        "data": d1, "shopee": 1, "ml": 0, "avulso": 1, "cancelados": 1, "valor_dia": Decimal("9.00"),
    }
    assert itens[1]["ml"] == 1 and itens[1]["valor_dia"] == Decimal("6.50")
    assert total == Decimal("15.50")
    assert totais_canais(itens) == {"shopee": 1, "ml": 1, "avulso": 1, "cancelados": 1}


def test_valor_final_soma_adicao_e_subtrai():
    assert valor_final_repasse("100.00", "10.50", "3.25") == Decimal("107.25")


def test_sobreposicao_de_periodo():
    existentes = [(date(2026, 11, 1), date(2026, 11, 15))]
    assert periodos_sobrepostos(existentes, date(2026, 11, 10), date(2026, 11, 20)) == existentes[0]
    assert periodos_sobrepostos(existentes, date(2026, 11, 16), date(2026, 11, 30)) is None
    assert periodos_sobrepostos([], date(2026, 11, 1), date(2026, 11, 15)) is None


def test_status_do_repasse():
    assert status_elegivel_pagamento("gerado") and status_elegivel_pagamento("REAJUSTADO")
    assert not status_elegivel_pagamento("PAGO")
    assert status_permite_reajuste("GERADO") and not status_permite_reajuste("PAGO")
