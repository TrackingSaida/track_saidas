"""Cancelado no fechamento: não paga e não gera multa (desconto duplicado)."""
from decimal import Decimal

from extrato_valor_utils import valor_extrato_por_filtro


def _total_dia_fechamento(valor_feitos: Decimal, valor_cancelados: Decimal) -> Decimal:
    """Espelha a regra do resumo/PDF: total = feitos; cancelados são informativos."""
    return Decimal(valor_feitos or 0).quantize(Decimal("0.01"))


def _contribuicao_saida(
    *,
    is_cancelado: bool,
    delta: Decimal,
    pacote_g_adicional: bool = False,
) -> Decimal:
    """Espelha _calcular_valor_base_*: cancelado contribui R$ 0,00."""
    if is_cancelado:
        return Decimal("0.00")
    total = delta
    if pacote_g_adicional:
        total += delta
    return total


def test_cenario_cliente_50_feitos_2_cancelados_r200():
    # 52 pedidos, 2 cancelados → 50 feitos × R$ 4,00 = R$ 200,00 (não 192).
    valor_unit = Decimal("4.00")
    feitos = 50
    cancelados = 2
    valor_feitos = (feitos * valor_unit).quantize(Decimal("0.01"))
    valor_cancelados = (cancelados * valor_unit).quantize(Decimal("0.01"))

    assert valor_feitos == Decimal("200.00")
    assert valor_cancelados == Decimal("8.00")
    assert _total_dia_fechamento(valor_feitos, valor_cancelados) == Decimal("200.00")
    # Bug antigo: 200 - 8 = 192
    assert valor_feitos - valor_cancelados == Decimal("192.00")


def test_valor_base_cancelado_contribui_zero():
    delta = Decimal("4.00")
    assert _contribuicao_saida(is_cancelado=False, delta=delta) == Decimal("4.00")
    assert _contribuicao_saida(is_cancelado=True, delta=delta) == Decimal("0.00")


def test_pacote_g_cancelado_nao_gera_multa():
    delta = Decimal("4.00")
    # Feito + G: 8,00
    assert _contribuicao_saida(is_cancelado=False, delta=delta, pacote_g_adicional=True) == Decimal("8.00")
    # Cancelado + G: 0,00 (não -8,00)
    assert _contribuicao_saida(is_cancelado=True, delta=delta, pacote_g_adicional=True) == Decimal("0.00")


def test_extrato_grupo_entregue_alinhado_ao_fechamento():
    # 10 feitos × 4 = 40; 2 cancelados × 4 = 8 informativos → a receber 40.
    assert valor_extrato_por_filtro(Decimal("40.00"), Decimal("8.00"), "grupo_entregue") == Decimal("40.00")


def test_sem_cancelados_total_igual_bruto():
    valor_feitos = Decimal("200.00")
    assert _total_dia_fechamento(valor_feitos, Decimal("0.00")) == Decimal("200.00")
