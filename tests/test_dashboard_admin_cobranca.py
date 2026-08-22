"""Testes da base de cobrança do Indicador Admin."""
from decimal import Decimal

from dashboard_admin_cobranca import (
    agregar_cobranca_por_owner,
    detalhe_base_cobranca,
    origem_cobranca_pacote,
    receita_admin_owner,
    tipo_operacao_owner,
)


def test_tipo_operacao_owner():
    assert tipo_operacao_owner(ignorar_coleta=True, entrada_habilitada=False) == "Só Saída"
    assert tipo_operacao_owner(ignorar_coleta=False, entrada_habilitada=False) == "Coleta"
    assert tipo_operacao_owner(ignorar_coleta=True, entrada_habilitada=True) == "Entrada"
    assert tipo_operacao_owner(ignorar_coleta=False, entrada_habilitada=True) == "Coleta + Entrada"


def test_origem_coleta_saida_conta_uma_vez():
    assert origem_cobranca_pacote(
        teve_coleta_no_periodo=True,
        primeira_entrada_no_periodo=False,
        saida_valida_no_periodo=True,
        teve_coleta_alguma_vez=True,
        teve_entrada_alguma_vez=False,
    ) == "coleta"


def test_origem_entrada_saida_conta_uma_vez():
    assert origem_cobranca_pacote(
        teve_coleta_no_periodo=False,
        primeira_entrada_no_periodo=True,
        saida_valida_no_periodo=True,
        teve_coleta_alguma_vez=False,
        teve_entrada_alguma_vez=True,
    ) == "entrada"


def test_origem_coleta_entrada_saida_prioriza_coleta():
    assert origem_cobranca_pacote(
        teve_coleta_no_periodo=True,
        primeira_entrada_no_periodo=True,
        saida_valida_no_periodo=True,
        teve_coleta_alguma_vez=True,
        teve_entrada_alguma_vez=True,
    ) == "coleta"


def test_origem_avulso_so_saida():
    assert origem_cobranca_pacote(
        teve_coleta_no_periodo=False,
        primeira_entrada_no_periodo=False,
        saida_valida_no_periodo=True,
        teve_coleta_alguma_vez=False,
        teve_entrada_alguma_vez=False,
    ) == "saida"


def test_origem_saida_nao_cobra_se_ja_teve_entrada():
    assert origem_cobranca_pacote(
        teve_coleta_no_periodo=False,
        primeira_entrada_no_periodo=False,
        saida_valida_no_periodo=True,
        teve_coleta_alguma_vez=False,
        teve_entrada_alguma_vez=True,
    ) is None


def test_origem_saida_nao_cobra_se_ja_teve_coleta():
    assert origem_cobranca_pacote(
        teve_coleta_no_periodo=False,
        primeira_entrada_no_periodo=False,
        saida_valida_no_periodo=True,
        teve_coleta_alguma_vez=True,
        teve_entrada_alguma_vez=False,
    ) is None


def test_quinzena_mista_saidas_antes_e_entrada_depois():
    """Dias 01-09 só saída; dia 10+ entrada. Pacotes distintos, sem duplicar."""
    # saida dia 09: sem coleta/entrada
    assert origem_cobranca_pacote(
        teve_coleta_no_periodo=False,
        primeira_entrada_no_periodo=False,
        saida_valida_no_periodo=True,
        teve_coleta_alguma_vez=False,
        teve_entrada_alguma_vez=False,
    ) == "saida"
    # entrada dia 10 + saida depois
    assert origem_cobranca_pacote(
        teve_coleta_no_periodo=False,
        primeira_entrada_no_periodo=True,
        saida_valida_no_periodo=True,
        teve_coleta_alguma_vez=False,
        teve_entrada_alguma_vez=True,
    ) == "entrada"
    # coleta dia 12 + saida
    assert origem_cobranca_pacote(
        teve_coleta_no_periodo=True,
        primeira_entrada_no_periodo=False,
        saida_valida_no_periodo=True,
        teve_coleta_alguma_vez=True,
        teve_entrada_alguma_vez=False,
    ) == "coleta"


def test_agregar_nao_duplica_mesmo_codigo():
    agg = agregar_cobranca_por_owner(
        ["Giro"],
        coleta_ids=[(1, "Giro")],
        entrada_ids=[(1, "Giro"), (2, "Giro")],
        saida_ids=[(1, "Giro"), (2, "Giro"), (3, "Giro")],
    )
    item = agg["Giro"]
    assert item.cobranca_coleta == 1
    assert item.cobranca_entrada == 1
    assert item.cobranca_saida == 1
    assert item.base_cobranca == 3


def test_agregar_quinzena_mista_por_owner():
    agg = agregar_cobranca_por_owner(
        ["Giro"],
        coleta_ids=[(10, "Giro"), (11, "Giro")],
        entrada_ids=[(20, "Giro")],
        saida_ids=[(1, "Giro"), (2, "Giro"), (3, "Giro")],
    )
    item = agg["Giro"]
    assert item.base_cobranca == 6
    assert detalhe_base_cobranca(
        item.cobranca_coleta, item.cobranca_entrada, item.cobranca_saida
    ) == "2 coleta + 1 entrada + 3 só saída"


def test_receita_usa_valor_do_owner():
    assert receita_admin_owner(Decimal("0.05"), 391) == Decimal("19.55")


def test_detalhe_vazio():
    assert detalhe_base_cobranca(0, 0, 0) == "0 pacotes"
