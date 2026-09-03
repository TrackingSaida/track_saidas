"""Contagem de NA_BASE por marketplace (Indicadores)."""
from types import SimpleNamespace

from entrada_na_base_pure import (
    classify_servico_na_base,
    contar_ainda_na_base_por_marketplace,
    contar_cancelados_apos_entrada_por_marketplace,
)


def test_classify_servico_na_base():
    assert classify_servico_na_base("Shopee") == "shopee"
    assert classify_servico_na_base("Mercado Livre") == "mercado_livre"
    assert classify_servico_na_base("Flex") == "mercado_livre"
    assert classify_servico_na_base("ML") == "mercado_livre"
    assert classify_servico_na_base("Avulso") == "avulso"
    assert classify_servico_na_base(None) == "avulso"


def test_contar_ainda_na_base_por_marketplace():
    rows = [
        SimpleNamespace(servico="Shopee"),
        SimpleNamespace(servico="Mercado Livre"),
        SimpleNamespace(servico="Mercado Livre"),
        SimpleNamespace(servico="Mercado Livre"),
        SimpleNamespace(servico="Mercado Livre"),
        SimpleNamespace(servico="Mercado Livre"),
        SimpleNamespace(servico="Mercado Livre"),
        SimpleNamespace(servico="Mercado Livre"),
        SimpleNamespace(servico="Avulso"),
    ]
    counts = contar_ainda_na_base_por_marketplace(rows)
    assert counts == {"shopee": 1, "mercado_livre": 7, "avulso": 1}
    assert sum(counts.values()) == 9


def test_contar_ainda_na_base_por_marketplace_vazio():
    assert contar_ainda_na_base_por_marketplace([]) == {
        "shopee": 0,
        "mercado_livre": 0,
        "avulso": 0,
    }


def test_contar_cancelados_apos_entrada_por_marketplace():
    primeira_entrada = {
        1: ("Shopee", None),
        2: ("Mercado Livre", None),
        3: ("Mercado Livre", None),
        4: ("Mercado Livre", None),
        5: ("Avulso", None),
    }
    counts = contar_cancelados_apos_entrada_por_marketplace(primeira_entrada, {2, 3, 4})
    assert counts == {"shopee": 0, "mercado_livre": 3, "avulso": 0}
