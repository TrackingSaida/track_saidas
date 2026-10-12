from decimal import Decimal

from contabilidade_balanco_pure import (
    FAIXA_GERADO,
    FAIXA_PAGO,
    FAIXA_RECEBIDO,
    faixa_despesa,
    faixa_receita,
    montar_lado,
    resultado_previsto,
    resultado_realizado,
    variacao_pct,
)


def test_faixa_receita_agrupa_gerado_reajustado_e_legado():
    assert faixa_receita("GERADO") == FAIXA_GERADO
    assert faixa_receita("reajustado") == FAIXA_GERADO
    assert faixa_receita("FECHADO") == FAIXA_GERADO
    assert faixa_receita("RECEBIDO") == FAIXA_RECEBIDO
    assert faixa_receita("PAGO") is None
    assert faixa_receita(None) is None


def test_faixa_despesa_agrupa_gerado_reajustado_e_legado():
    assert faixa_despesa("GERADO") == FAIXA_GERADO
    assert faixa_despesa("REAJUSTADO") == FAIXA_GERADO
    assert faixa_despesa("fechado") == FAIXA_GERADO
    assert faixa_despesa("PAGO") == FAIXA_PAGO
    assert faixa_despesa("RECEBIDO") is None
    assert faixa_despesa("") is None


def test_montar_lado_soma_as_tres_faixas():
    lado = montar_lado("100.006", Decimal("50"), 25)
    assert lado["pendente"] == Decimal("100.01")
    assert lado["gerado"] == Decimal("50.00")
    assert lado["baixado"] == Decimal("25.00")
    assert lado["total"] == Decimal("175.01")


def test_previsto_considera_tudo_e_realizado_so_o_baixado():
    receber = montar_lado(1000, 2000, 3000)
    pagar = montar_lado(400, 800, 1200)
    assert resultado_previsto(receber, pagar) == Decimal("3600.00")
    assert resultado_realizado(receber, pagar) == Decimal("1800.00")


def test_realizado_negativo_quando_paga_antes_de_receber():
    receber = montar_lado(500, 1500, 0)
    pagar = montar_lado(0, 0, 700)
    assert resultado_previsto(receber, pagar) == Decimal("1300.00")
    assert resultado_realizado(receber, pagar) == Decimal("-700.00")


def test_periodo_vazio_zera_resultados():
    receber = montar_lado(None, None, None)
    pagar = montar_lado(0, 0, 0)
    assert receber["total"] == Decimal("0.00")
    assert resultado_previsto(receber, pagar) == Decimal("0.00")
    assert resultado_realizado(receber, pagar) == Decimal("0.00")


def test_variacao_pct_sobre_valor_absoluto_anterior():
    assert variacao_pct(150, 100) == Decimal("50.00")
    assert variacao_pct(-50, -100) == Decimal("50.00")
    assert variacao_pct(-150, -100) == Decimal("-50.00")
    assert variacao_pct(10, 0) is None
