"""Valor base em lote (resumo/A Pagar): mesmo resultado do cálculo por motoboy."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import entregador_routes as er

INI, FIM = date(2026, 9, 16), date(2026, 9, 30)
PRECOS = {
    1: {"shopee_valor": Decimal("2.50"), "ml_valor": Decimal("3.00"), "avulso_valor": Decimal("4.00"), "coleta_valor": Decimal("0")},
    2: {"shopee_valor": Decimal("5.00"), "ml_valor": Decimal("6.00"), "avulso_valor": Decimal("7.00"), "coleta_valor": Decimal("0")},
}


def _saida(mid, servico, status="entregue", grande=False, categoria=None):
    return SimpleNamespace(motoboy_id=mid, servico=servico, status=status, is_grande=grande, categoria_g_id=categoria)


def _preparar(monkeypatch, chamadas):
    def precos(db, sub_base, motoboy_id=None):
        chamadas.append(motoboy_id)
        return PRECOS[motoboy_id]

    monkeypatch.setattr(er, "resolver_precos_motoboy", precos)
    monkeypatch.setattr(er, "_toggle_pacote_g_ativo", lambda db, sb: True)
    monkeypatch.setattr(er, "_regras_g_motoboy", lambda db, sb: ({10: {"modo": "valor_fixo", "valor": "1.50"}}, 10))


def test_lote_soma_por_motoboy_sem_cancelados_e_com_g(monkeypatch):
    chamadas = []
    _preparar(monkeypatch, chamadas)
    saidas = [
        _saida(1, "shopee"),
        _saida(1, "mercado_livre", grande=True),
        _saida(1, "avulso", status="cancelado", grande=True),
        _saida(2, "avulso"),
        _saida(2, "shopee", grande=True, categoria=10),
        _saida(3, "shopee"),
        _saida(None, "shopee"),
    ]

    valores = er._calcular_valores_base_motoboys_periodo(
        None, "sb", [1, 2], INI, FIM, precos_cache={}, saidas_periodo=saidas
    )

    assert valores == {1: Decimal("7.00"), 2: Decimal("13.50")}
    assert sorted(chamadas) == [1, 2]


def test_lote_motoboy_sem_saidas_fica_zero(monkeypatch):
    _preparar(monkeypatch, [])
    valores = er._calcular_valores_base_motoboys_periodo(
        None, "sb", [1, 2], INI, FIM, saidas_periodo=[_saida(1, "shopee")]
    )
    assert valores == {1: Decimal("2.50"), 2: Decimal("0.00")}


def test_lote_igual_ao_calculo_individual(monkeypatch):
    _preparar(monkeypatch, [])
    saidas = [_saida(1, "shopee", grande=True), _saida(2, "mercado_livre"), _saida(2, "avulso", status="cancelado")]
    lote = er._calcular_valores_base_motoboys_periodo(None, "sb", [1, 2], INI, FIM, saidas_periodo=saidas)
    for mid in (1, 2):
        individual = er._calcular_valores_base_motoboys_periodo(
            None, "sb", [mid], INI, FIM, saidas_periodo=[s for s in saidas if s.motoboy_id == mid]
        )
        assert individual[mid] == lote[mid]


def test_precos_cache_resolve_uma_vez(monkeypatch):
    chamadas = []
    _preparar(monkeypatch, chamadas)
    cache = {}
    er.precos_motoboy_cache(None, "sb", 1, cache)
    er.precos_motoboy_cache(None, "sb", 1, cache)
    assert chamadas == [1]
