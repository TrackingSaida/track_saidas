from decimal import Decimal
from types import SimpleNamespace

from base_fechamento_routes import (
    _g_categorias_item,
    _qtd_g_com_cobranca,
    _valor_g_categorias,
)
from coletas import ColetaManualCreate, ColetaManualUpdate
from base_fechamento_status_pure import (
    MOTIVO_PACOTES_G,
    fechamento_base_divergente,
    motivo_bloqueio_lote_base,
)
from fechamento_valor_pure import adicional_g, valor_g_adicional, valor_saida
from pacote_g_categorias import categoria_da_saida, g_categorias_com_padrao

PRECOS_MOTOBOY = {"shopee_valor": "5.00", "ml_valor": "6.00", "avulso_valor": "8.00"}
PRECOS_BASE = {"shopee": Decimal("3.00"), "ml": Decimal("4.00"), "avulso": Decimal("10.00")}
G1 = {"modo": "multiplicador", "valor": Decimal("1.00")}
G2 = {"modo": "multiplicador", "valor": Decimal("2.00")}
G3 = {"modo": "valor_fixo", "valor": Decimal("7.50")}


def test_adicional_g_multiplicador_e_valor_fixo():
    assert adicional_g(Decimal("5.00"), G1) == Decimal("5.00")
    assert adicional_g(Decimal("5.00"), G2) == Decimal("10.00")
    assert adicional_g(Decimal("5.00"), G3) == Decimal("7.50")


def test_adicional_g_sem_regra_mantem_um_servico():
    assert adicional_g(Decimal("5.00"), None) == Decimal("5.00")


def test_valor_saida_soma_adicional_da_categoria():
    assert valor_saida(PRECOS_MOTOBOY, "Shopee", "entregue", True, True, G2) == Decimal("15.00")
    assert valor_saida(PRECOS_MOTOBOY, "Shopee", "entregue", True, True, G3) == Decimal("12.50")


def test_valor_saida_cancelado_e_adicional_desligado():
    assert valor_saida(PRECOS_MOTOBOY, "Shopee", "cancelado", True, True, G2) == Decimal("0.00")
    assert valor_saida(PRECOS_MOTOBOY, "Shopee", "entregue", True, False, G2) == Decimal("5.00")


def test_valor_g_adicional_por_categoria():
    g = {(1, "shopee"): 2, (2, "flex"): 1, (3, "avulso"): 1}
    regras = {1: G1, 2: G2, 3: G3}
    # 2x5 + 1x(6x2) + 7.50
    assert valor_g_adicional(g, PRECOS_MOTOBOY, True, regras) == Decimal("29.50")
    assert valor_g_adicional(g, PRECOS_MOTOBOY, False, regras) == Decimal("0.00")


def test_categoria_da_saida_legado_cai_na_padrao():
    assert categoria_da_saida(SimpleNamespace(is_grande=True, categoria_g_id=None), 9) == 9
    assert categoria_da_saida(SimpleNamespace(is_grande=True, categoria_g_id=4), 9) == 4
    assert categoria_da_saida(SimpleNamespace(is_grande=False, categoria_g_id=4), 9) is None


def test_g_categorias_com_padrao_para_coleta_antiga():
    assert g_categorias_com_padrao(None, 1, 0, 2, 5) == {"5": {"shopee": 1, "ml": 0, "avulso": 2}}
    assert g_categorias_com_padrao({"7": {"ml": 3}}, 1, 0, 2, 5) == {"7": {"shopee": 0, "ml": 3, "avulso": 0}}
    assert g_categorias_com_padrao(None, 0, 0, 0, 5) == {}


def test_valor_g_base_segue_regra_cobrar_da_base():
    regras = {1: G2, 2: G3, 3: {"modo": "valor_fixo", "valor": Decimal("0")}}
    g_cats = {"1": {"shopee": 1}, "2": {"avulso": 2}, "3": {"ml": 4}}
    # 1x(3x2) + 2x7.50 + 0
    assert _valor_g_categorias(g_cats, PRECOS_BASE, regras) == Decimal("21.00")
    assert _qtd_g_com_cobranca(g_cats, regras) == 3


def test_valor_g_base_categoria_desconhecida_nao_cobra():
    assert _valor_g_categorias({"99": {"shopee": 3}}, PRECOS_BASE, {}) == Decimal("0.00")


def test_g_categorias_item_aceita_dict_e_objeto():
    item = {"g_categorias": None, "g_shopee": 2, "g_ml": 0, "g_avulso": 0}
    assert _g_categorias_item(item, 1) == {"1": {"shopee": 2, "ml": 0, "avulso": 0}}
    obj = SimpleNamespace(g_categorias={"3": {"avulso": 1}}, g_shopee=0, g_ml=0, g_avulso=1)
    assert _g_categorias_item(obj, 1) == {"3": {"shopee": 0, "ml": 0, "avulso": 1}}


def test_lote_libera_g_com_regra_de_cobranca():
    kw = {"status_fechamento": None, "qtd_dias": 2, "qtd_dias_pendentes": 0, "total_pacotes_g": 3}
    assert motivo_bloqueio_lote_base(**kw) == MOTIVO_PACOTES_G
    assert motivo_bloqueio_lote_base(**kw, pacotes_g_com_regra=2) == MOTIVO_PACOTES_G
    assert motivo_bloqueio_lote_base(**kw, pacotes_g_com_regra=3) is None


def test_divergencia_considera_valor_g():
    kw = dict(
        bruto_gravado=Decimal("100"), cancelados_gravado=Decimal("0"), g_gravado=(1, 0, 0),
        bruto_atual=Decimal("100"), cancelados_atual=Decimal("0"), g_atual=(1, 0, 0),
    )
    assert fechamento_base_divergente(**kw) is False
    assert fechamento_base_divergente(**kw, valor_g_gravado=Decimal("0"), valor_g_atual=Decimal("0.00")) is False
    assert fechamento_base_divergente(**kw, valor_g_gravado=Decimal("5"), valor_g_atual=Decimal("6")) is True


def test_coleta_manual_aceita_g_por_categoria():
    body = ColetaManualCreate(
        data="2026-09-21",
        base="BASE1",
        shopee=10,
        g_categorias=[{"categoria_g_id": 4, "shopee": 2}],
    )
    assert body.g_categorias[0].categoria_g_id == 4
    assert body.g_categorias[0].shopee == 2 and body.g_categorias[0].ml == 0
    assert "g_categorias" not in ColetaManualUpdate(shopee=3).model_fields_set
    assert "g_categorias" in ColetaManualUpdate(g_categorias=[]).model_fields_set
