"""Regra de valor do extrato (Minhas entregas) por filtro de status."""
from datetime import date
from decimal import Decimal

from extrato_valor_utils import situacao_fechamento_periodo, valor_extrato_por_filtro


def test_todos_soma_entregue_mais_cancelado():
    # 10 x R$ 4,00 entregues + 2 x R$ 4,00 cancelados = R$ 48,00
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("8.00"), "todos") == Decimal("48.00")


def test_grupo_entregue_nao_desconta_cancelados_de_novo():
    # valor_feitos já exclui cancelados (10 x R$ 4,00); cancelados são só informativos.
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("8.00"), "grupo_entregue") == Decimal("40.00")


def test_cancelados_so_valor_cancelado():
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("8.00"), "cancelados") == Decimal("8.00")


def test_sem_cancelados_entregue_igual_bruto():
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("0.00"), "grupo_entregue") == Decimal("40.00")
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("0.00"), "todos") == Decimal("40.00")


def test_modo_desconhecido_usa_somente_feitos():
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("8.00"), "") == Decimal("40.00")


def test_mais_cancelados_que_entregues_nao_fica_negativo_no_filtro_entregue():
    # 1 feito + 2 cancelados: total a receber = só o feito (sem multa).
    assert valor_extrato_por_filtro(Decimal("4.00"), Decimal("8.00"), "grupo_entregue") == Decimal("4.00")
    assert valor_extrato_por_filtro(Decimal("4.00"), Decimal("8.00"), "todos") == Decimal("12.00")


INICIO = date(2026, 10, 1)
FIM = date(2026, 10, 15)


def test_situacao_sem_fechamento_no_periodo():
    fora = [(date(2026, 9, 16), date(2026, 9, 30), "PAGO")]
    assert situacao_fechamento_periodo(fora, INICIO, FIM) == "sem_fechamento"
    assert situacao_fechamento_periodo([], INICIO, FIM) == "sem_fechamento"


def test_situacao_gerado_reajustado_e_fechado_legado():
    for status in ("GERADO", "REAJUSTADO", "fechado"):
        assert situacao_fechamento_periodo([(INICIO, FIM, status)], INICIO, FIM) == "gerado"


def test_situacao_pago_cobrindo_todo_periodo():
    assert situacao_fechamento_periodo([(INICIO, FIM, "PAGO")], INICIO, FIM) == "pago"


def test_situacao_pago_parcial_continua_previsao():
    parcial = [(INICIO, date(2026, 10, 7), "PAGO")]
    assert situacao_fechamento_periodo(parcial, INICIO, FIM) == "gerado"
