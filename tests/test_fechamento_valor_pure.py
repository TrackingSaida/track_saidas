from decimal import Decimal
from types import SimpleNamespace

from fechamento_valor_pure import ajustes_mudaram, contar_g, valor_g_adicional, valor_saida

PRECOS = {
    "shopee_valor": Decimal("5.00"),
    "ml_valor": Decimal("7.00"),
    "avulso_valor": Decimal("10.00"),
}


def _saida(servico, status="entregue", grande=False):
    return SimpleNamespace(servico=servico, status=status, is_grande=grande)


def test_valor_saida_g_dobra_quando_adicional_ligado():
    assert valor_saida(PRECOS, "Shopee", "entregue", True, True) == Decimal("10.00")
    assert valor_saida(PRECOS, "Mercado Livre", "entregue", True, True) == Decimal("14.00")


def test_valor_saida_g_sem_adicional_vale_preco_normal():
    assert valor_saida(PRECOS, "avulso", "entregue", True, False) == Decimal("10.00")


def test_valor_saida_cancelado_vale_zero_mesmo_g():
    assert valor_saida(PRECOS, "Shopee", "cancelado", True, True) == Decimal("0.00")
    assert valor_saida(PRECOS, "Shopee", "Cancelados", False, False) == Decimal("0.00")


def test_contar_g_ignora_cancelados_e_nao_g():
    saidas = [
        _saida("Shopee", grande=True),
        _saida("Shopee", "cancelado", grande=True),
        _saida("ML", grande=True),
        _saida("avulso", grande=False),
        _saida("outro", grande=True),
    ]
    assert contar_g(saidas) == {"shopee": 1, "ml": 1, "avulso": 1, "total": 3}


def test_valor_g_adicional():
    g = {"shopee": 2, "flex": 1, "avulso": 0}
    assert valor_g_adicional(g, PRECOS, True) == Decimal("17.00")
    assert valor_g_adicional(g, PRECOS, False) == Decimal("0.00")


def test_ajustes_mudaram_detecta_valor_e_motivo():
    atual = {
        "valor_base": Decimal("100.00"),
        "valor_adicao": Decimal("0.00"),
        "motivo_adicao": None,
        "valor_subtracao": Decimal("0.00"),
        "motivo_subtracao": None,
    }
    assert not ajustes_mudaram(atual, {"valor_adicao": Decimal("0"), "motivo_adicao": ""})
    assert not ajustes_mudaram(atual, {"valor_base": Decimal("100.0")})
    assert ajustes_mudaram(atual, {"valor_adicao": Decimal("5.00")})
    assert ajustes_mudaram(atual, {"motivo_subtracao": "Avaria"})
    assert ajustes_mudaram(atual, {"valor_base": Decimal("120.00")})
