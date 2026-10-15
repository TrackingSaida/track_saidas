"""Contagem nos apps: só a diferença para o sistema entra no valor final."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from fechamento_valor_pure import (
    acerto_canal,
    ajustes_mudaram,
    snapshot_canal,
    valor_acerto_apps,
    valor_final_fechamento,
)

PRECOS = {"ml_valor": Decimal("8.50"), "shopee_valor": Decimal("7.20"), "avulso_valor": Decimal("11.50")}


def test_a_mais_soma_e_a_menos_desconta():
    assert acerto_canal(120, 115, Decimal("8.50")) == Decimal("42.50")
    assert acerto_canal(78, 80, Decimal("7.20")) == Decimal("-14.40")


def test_igual_ou_sem_quantidade_nao_mexe():
    assert acerto_canal(115, 115, Decimal("8.50")) == Decimal("0.00")
    assert acerto_canal(None, 115, Decimal("8.50")) == Decimal("0.00")


def test_preco_zero_nao_soma():
    assert acerto_canal(130, 115, Decimal("0")) == Decimal("0.00")
    assert acerto_canal(130, 115, None) == Decimal("0.00")


def test_snapshot_usa_preco_do_canal():
    flex = snapshot_canal("flex", 120, 115, PRECOS)
    shopee = snapshot_canal("shopee", 78, 80, PRECOS)
    assert flex == {"qtd_app_flex": 120, "qtd_sistema_flex": 115, "preco_acerto_flex": Decimal("8.50")}
    assert shopee["preco_acerto_shopee"] == Decimal("7.20")
    assert snapshot_canal("flex", None, 115, PRECOS) == {
        "qtd_app_flex": None, "qtd_sistema_flex": None, "preco_acerto_flex": None,
    }


def test_valor_final_inclui_acerto_sem_mexer_no_base():
    colunas = {**snapshot_canal("flex", 120, 115, PRECOS), **snapshot_canal("shopee", 78, 80, PRECOS)}
    acerto = valor_acerto_apps(colunas)
    assert acerto == Decimal("28.10")
    assert valor_final_fechamento(Decimal("1553.50"), acerto, Decimal("10"), Decimal("5")) == Decimal("1586.60")


def test_reajuste_so_compara_quantidade_que_veio():
    atual = {"valor_adicao": Decimal("0"), "qtd_app_flex": 120, "qtd_app_shopee": None}
    assert not ajustes_mudaram(atual, {"valor_adicao": Decimal("0")})
    assert not ajustes_mudaram(atual, {"qtd_app_flex": 120})
    assert ajustes_mudaram(atual, {"qtd_app_flex": 121})
    assert ajustes_mudaram(atual, {"qtd_app_flex": None})
    assert ajustes_mudaram(atual, {"qtd_app_shopee": 80})


def test_pdf_lista_so_canal_com_diferenca():
    from fechamento_pdf_service import linhas_contagem_apps

    fech = SimpleNamespace(
        qtd_app_flex=120, qtd_sistema_flex=115, preco_acerto_flex=Decimal("8.50"),
        qtd_app_shopee=80, qtd_sistema_shopee=80, preco_acerto_shopee=Decimal("7.20"),
    )
    linhas = linhas_contagem_apps(fech)
    assert len(linhas) == 1
    assert linhas[0][0].startswith("Mercado Livre · 5 a mais no app")
    assert linhas[0][1] == Decimal("42.50")


def test_quantidade_negativa_rejeitada():
    from entregador_fechamento_routes import FechamentoCreate

    with pytest.raises(ValidationError):
        FechamentoCreate(id_motoboy=1, periodo_inicio="2026-10-01", periodo_fim="2026-10-15", qtd_app_flex=-1)
