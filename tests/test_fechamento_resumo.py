"""Agregação do resumo de fechamento (mesmo recorte do relatório)."""
from decimal import Decimal

from fechamento_resumo_utils import (
    agregar_resumo_fechamento,
    base_filtro_ativo,
    base_prefiltro_sql_valores,
    saida_pertence_base,
)
from types import SimpleNamespace

from fechamento_valor_pure import normalizar_servico, saida_cancelada, valor_entregas_agregadas


def _tipo_servico_sql_espelho(servico: str | None) -> str:
    """Mesma regra do CASE em `_expr_tipo_servico` (sem dependência de banco)."""
    s = (servico or "").lower()
    if "shopee" in s:
        return "shopee"
    if "ml" in s or "mercado" in s:
        return "flex"
    return "avulso"


def test_tipo_servico_sql_bate_com_normalizar_python():
    amostras = [
        None,
        "",
        "Shopee",
        "ML Flex",
        "Mercado Livre",
        "Avulso",
        "flex only",
        "email",
        "SHOPEE Express",
    ]
    for s in amostras:
        assert _tipo_servico_sql_espelho(s) == normalizar_servico(s)


def test_valor_agregado_soma_quantidade_e_ignora_cancelado():
    linhas = [
        SimpleNamespace(motoboy_id=7, tipo_servico="shopee", qtd=3, is_cancelado=False, is_grande=True, categoria_g_id=None),
        SimpleNamespace(motoboy_id=7, tipo_servico="shopee", qtd=2, is_cancelado=True, is_grande=False, categoria_g_id=None),
        SimpleNamespace(motoboy_id=8, tipo_servico="flex", qtd=1, is_cancelado=False, is_grande=False, categoria_g_id=None),
    ]
    precos = {
        7: {"shopee_valor": Decimal("4.00"), "ml_valor": Decimal("5.00"), "avulso_valor": Decimal("6.00")},
        8: {"shopee_valor": Decimal("4.00"), "ml_valor": Decimal("5.00"), "avulso_valor": Decimal("6.00")},
    }
    totais = valor_entregas_agregadas(
        linhas, precos, toggle_pacote_g=True, regras_g={}, padrao_g=1,
    )
    # 3 shopee + 3 adicionais G (sem regra = +1x) ; cancelados fora
    assert totais[7] == Decimal("24.00")
    assert totais[8] == Decimal("5.00")


def test_cancelado_sql_like_bate_com_python():
    for status in (None, "", "entregue", "Cancelado", "CANCELADO_CLIENTE", "em_rota"):
        py = saida_cancelada(status)
        sql = "cancel" in (status or "").lower()
        assert py is sql


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


def test_base_filtro_hifen_e_espacos_repetidos_sao_o_mesmo_nome():
    alvo = base_filtro_ativo("CLEBER  GABRIEL")
    assert alvo == "cleber gabriel"
    assert saida_pertence_base("CLEBER - GABRIEL", alvo) is True
    assert saida_pertence_base("CLEBER GABRIEL", alvo) is True
    assert saida_pertence_base("cleber-gabriel", alvo) is True


def test_base_prefiltro_sql_usa_chave_normalizada():
    assert base_prefiltro_sql_valores("  São José ") == ["sao jose"]
    assert base_prefiltro_sql_valores("(Todas)") == []
    assert base_prefiltro_sql_valores("CLEBER - GABRIEL") == ["cleber gabriel"]
    assert base_prefiltro_sql_valores("CLEBER  GABRIEL") == ["cleber gabriel"]


def test_encerrado_pelo_sistema_entra_no_fechamento_e_nao_e_cancelado():
    import ast
    from pathlib import Path

    arvore = ast.parse(Path(__file__).resolve().parents[1].joinpath("entregador_routes.py").read_text())
    lista = None
    for no in arvore.body:
        if isinstance(no, ast.Assign):
            for alvo in no.targets:
                if isinstance(alvo, ast.Name) and alvo.id == "STATUS_VALOR_BASE_VALIDOS":
                    lista = ast.literal_eval(no.value)
    assert lista is not None
    for status in ("encerrado_sistema", "encerrado pelo sistema"):
        assert status in lista
        assert saida_cancelada(status) is False
        assert saida_cancelada(status.upper()) is False
