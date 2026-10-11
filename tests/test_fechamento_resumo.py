"""Agregação do resumo de fechamento (mesmo recorte do relatório)."""
from decimal import Decimal

from fechamento_resumo_utils import agregar_resumo_fechamento, base_filtro_ativo, saida_pertence_base


def test_resumo_exemplo_relatorio():
    itens = [
        {
            "shopee": 400,
            "flex": 100,
            "avulso": 60,
            "cancel_shopee": 0,
            "cancel_flex": 0,
            "cancel_avulso": 0,
            "g_total": 0,
            "valor_shopee": Decimal("1600.00"),
            "valor_flex": Decimal("400.00"),
            "valor_avulso": Decimal("240.00"),
            "valor_cancel_shopee": Decimal("0.00"),
            "valor_cancel_flex": Decimal("0.00"),
            "valor_cancel_avulso": Decimal("0.00"),
            "valor_feitos": Decimal("2240.00"),
            "valor_cancelados": Decimal("0.00"),
        }
    ]
    resumo = agregar_resumo_fechamento(itens, valor_adicao=Decimal("4.00"), valor_subtracao=Decimal("0.00"))
    assert resumo["feitos"] == 560
    assert resumo["cancelados"] == 0
    assert resumo["pacotes_grandes"] == 0
    assert resumo["valor_bruto"] == Decimal("2240.00")
    assert resumo["valor_cancelados"] == Decimal("0.00")
    assert resumo["ajustes"] == Decimal("4.00")
    assert resumo["por_servico"]["shopee"]["feitos"] == 400
    assert resumo["por_servico"]["shopee"]["valor_feitos"] == Decimal("1600.00")
    assert resumo["por_servico"]["flex"]["feitos"] == 100
    assert resumo["por_servico"]["avulso"]["feitos"] == 60


def test_resumo_desconta_cancelados_por_servico():
    itens = [
        {
            "shopee": 10,
            "flex": 0,
            "avulso": 0,
            "cancel_shopee": 2,
            "cancel_flex": 0,
            "cancel_avulso": 0,
            "g_total": 1,
            "valor_shopee": Decimal("40.00"),
            "valor_flex": Decimal("0.00"),
            "valor_avulso": Decimal("0.00"),
            "valor_cancel_shopee": Decimal("8.00"),
            "valor_cancel_flex": Decimal("0.00"),
            "valor_cancel_avulso": Decimal("0.00"),
            "valor_feitos": Decimal("40.00"),
            "valor_cancelados": Decimal("8.00"),
        }
    ]
    resumo = agregar_resumo_fechamento(itens, valor_adicao=0, valor_subtracao=Decimal("1.50"))
    assert resumo["feitos"] == 10
    assert resumo["cancelados"] == 2
    assert resumo["pacotes_grandes"] == 1
    assert resumo["valor_bruto"] == Decimal("40.00")
    assert resumo["valor_cancelados"] == Decimal("8.00")
    assert resumo["ajustes"] == Decimal("-1.50")
    assert resumo["por_servico"]["shopee"]["cancelados"] == 2
    assert resumo["por_servico"]["shopee"]["valor_cancelados"] == Decimal("8.00")


def test_resumo_vazio():
    resumo = agregar_resumo_fechamento([])
    assert resumo["feitos"] == 0
    assert resumo["cancelados"] == 0
    assert resumo["valor_bruto"] == Decimal("0.00")
    assert resumo["ajustes"] == Decimal("0.00")
    assert resumo["por_servico"]["flex"]["feitos"] == 0


def test_base_filtro_vazio_ou_todas_nao_filtra():
    for valor in (None, "", "   ", "(Todas)", "(todas)"):
        assert base_filtro_ativo(valor) is None
        assert saida_pertence_base("CENTRO", base_filtro_ativo(valor)) is True
        assert saida_pertence_base(None, base_filtro_ativo(valor)) is True


def test_base_filtro_ignora_acento_maiuscula_e_espacos():
    alvo = base_filtro_ativo("  São José ")
    assert saida_pertence_base("SAO JOSE", alvo) is True
    assert saida_pertence_base("são josé", alvo) is True


def test_base_filtro_exclui_outra_base_e_sem_base():
    alvo = base_filtro_ativo("Centro")
    assert saida_pertence_base("Centro Sul", alvo) is False
    assert saida_pertence_base(None, alvo) is False
    assert saida_pertence_base("", alvo) is False
