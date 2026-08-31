"""Regra de valor do extrato (Minhas entregas) por filtro de status."""
from decimal import Decimal

from extrato_valor_utils import valor_extrato_por_filtro


def test_todos_soma_entregue_mais_cancelado():
    # 10 x R$ 4,00 entregues + 2 x R$ 4,00 cancelados = R$ 48,00
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("8.00"), "todos") == Decimal("48.00")


def test_entregue_subtrai_cancelados():
    # 10 x R$ 4,00 - 2 x R$ 4,00 = R$ 32,00
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("8.00"), "grupo_entregue") == Decimal("32.00")


def test_cancelados_so_valor_cancelado():
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("8.00"), "cancelados") == Decimal("8.00")


def test_sem_cancelados_entregue_igual_bruto():
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("0.00"), "grupo_entregue") == Decimal("40.00")
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("0.00"), "todos") == Decimal("40.00")


def test_modo_desconhecido_usa_entregue_menos_cancelado():
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("8.00"), "") == Decimal("32.00")


def test_mais_cancelados_que_entregues_fica_negativo_no_filtro_entregue():
    assert valor_extrato_por_filtro(Decimal("4.00"), Decimal("8.00"), "grupo_entregue") == Decimal("-4.00")
    assert valor_extrato_por_filtro(Decimal("4.00"), Decimal("8.00"), "todos") == Decimal("12.00")
