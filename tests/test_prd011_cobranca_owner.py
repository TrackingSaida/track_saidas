"""PRD-011 Fatia 3: data de faturamento, parceria, escopo de preço e modo de dois preços."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from owner_cobranca_engine import (
    EventoCodigo,
    LancamentoManual,
    PrecoFaixa,
    apurar,
    calcular_hash,
    modo_dual,
    papel_preco_item,
    preco_faixa_vigente,
)
from owner_cobranca_faturamento_pure import (
    calcular_data_faturamento,
    decidir_data_parceria,
    estado_faturamento,
    normalizar_dias_faturamento,
    rotulo_faturar_em,
)

INI = date(2026, 11, 1)
FIM = date(2026, 11, 15)
D5 = date(2026, 11, 5)
D6 = date(2026, 11, 6)
PRECO_BASES = Decimal("0.50")
PRECO_SELLERS = Decimal("0.80")
PAPEIS = {"LOJA SELLER": "seller", "BASE PARCEIRA": "base_parceira"}


def ev(id_saida, codigo, **kw):
    kw.setdefault("servico", "Shopee")
    return EventoCodigo(id_saida=id_saida, codigo=codigo, **kw)


def run_dual(modelo, eventos, **kw):
    return apurar(
        modelo=modelo,
        periodo_inicio=INI,
        periodo_fim=FIM,
        preco=PRECO_BASES,
        eventos=eventos,
        dual=True,
        preco_sellers=PRECO_SELLERS,
        papeis_por_base=PAPEIS,
        **kw,
    )


# ---- data de faturamento --------------------------------------------------
def test_data_faturamento_owner_com_15_dias():
    # 01–15 fatura dia 30; 16–fim fatura dia 15 do mês seguinte.
    assert calcular_data_faturamento(date(2026, 11, 15), 15) == date(2026, 11, 30)
    assert calcular_data_faturamento(date(2026, 11, 30), 15) == date(2026, 12, 15)


def test_data_faturamento_owner_com_5_dias():
    assert calcular_data_faturamento(date(2026, 11, 15), 5) == date(2026, 11, 20)
    assert calcular_data_faturamento(date(2026, 11, 30), 5) == date(2026, 12, 5)


def test_data_faturamento_fevereiro_soma_calendario():
    assert calcular_data_faturamento(date(2026, 2, 15), 15) == date(2026, 3, 2)


def test_data_faturamento_sem_prazo_e_zero_dias():
    assert calcular_data_faturamento(FIM, None) is None
    assert calcular_data_faturamento(FIM, 0) == FIM


def test_normalizar_dias_faturamento():
    assert normalizar_dias_faturamento("15") == 15
    assert normalizar_dias_faturamento(0) == 0
    assert normalizar_dias_faturamento(None) is None
    assert normalizar_dias_faturamento("") is None
    with pytest.raises(ValueError):
        normalizar_dias_faturamento(-1)
    with pytest.raises(ValueError):
        normalizar_dias_faturamento("x")


def test_estado_faturamento():
    hoje = date(2026, 11, 30)
    assert estado_faturamento(date(2026, 12, 5), hoje, gerada=False) == "agendado"
    assert estado_faturamento(hoje, hoje, gerada=False) == "faturar_hoje"
    assert estado_faturamento(date(2026, 11, 20), hoje, gerada=False) == "atrasado"
    assert estado_faturamento(date(2026, 11, 20), hoje, gerada=True) == "gerado"
    assert estado_faturamento(None, hoje, gerada=False) == "sem_prazo"


def test_rotulo_faturar_em():
    assert rotulo_faturar_em(date(2026, 11, 30), 15) == "30/11 · 15 dias"
    assert rotulo_faturar_em(date(2026, 11, 16), 1) == "16/11 · 1 dia"
    assert rotulo_faturar_em(None, 15) is None


# ---- data de parceria -----------------------------------------------------
def test_data_parceria_informada_uma_vez():
    primeira = date(2024, 3, 12)
    assert decidir_data_parceria(None, primeira) == primeira
    assert decidir_data_parceria(primeira, primeira) == primeira
    assert decidir_data_parceria(primeira, None) == primeira
    with pytest.raises(ValueError):
        decidir_data_parceria(primeira, date(2025, 1, 1))


# ---- papel do preço -------------------------------------------------------
def test_modo_dual_exige_os_dois_papeis():
    assert modo_dual(["seller", "base_parceira"]) is True
    assert modo_dual(["seller", "seller"]) is False
    assert modo_dual(["base_parceira", None]) is False
    assert modo_dual([]) is False


def test_papel_preco_coleta_segue_cliente_e_complementar_usa_bases():
    assert papel_preco_item("coleta", "Loja Seller ", PAPEIS) == "sellers"
    assert papel_preco_item("manual", "BASE PARCEIRA", PAPEIS) == "bases"
    assert papel_preco_item("coleta", "Desconhecida", PAPEIS) == "bases"
    assert papel_preco_item("entrada", "LOJA SELLER", PAPEIS) == "bases"
    assert papel_preco_item("saida", "LOJA SELLER", PAPEIS) == "bases"


# ---- apuração com dois preços ----------------------------------------------
def test_dual_cobra_cada_lado_com_seu_preco_e_lado_zerado_aparece():
    ap = run_dual("coleta_saida", [
        ev(1, "S1", base="LOJA SELLER", data_coleta=D5, id_coleta=10, status="coletado"),
        ev(2, "S2", base="LOJA SELLER", data_coleta=D5, id_coleta=10, status="coletado"),
        ev(3, "B1", base="BASE PARCEIRA", data_coleta=D5, id_coleta=11, status="coletado"),
    ])
    assert (ap.qtd_sellers, ap.qtd_bases) == (2, 1)
    assert ap.valor_sellers == Decimal("1.60")
    assert ap.valor_bases == Decimal("0.50")
    assert ap.valor_pacotes == Decimal("2.10")
    assert ap.valor_total == Decimal("2.10")
    assert ap.dual is True and ap.preco_sellers == PRECO_SELLERS

    so_sellers = run_dual("coleta_saida", [
        ev(1, "S1", base="LOJA SELLER", data_coleta=D5, id_coleta=10, status="coletado"),
    ])
    assert (so_sellers.qtd_bases, so_sellers.valor_bases) == (0, Decimal("0.00"))
    assert so_sellers.valor_total == Decimal("0.80")


def test_dual_pacote_coletado_em_seller_e_enviado_a_base_cobra_uma_vez_preco_seller():
    ap = run_dual("coleta_saida", [
        ev(1, "S1", base="LOJA SELLER", data_coleta=D5, id_coleta=10,
           status="enviado_para_base", data_saida=D6),
    ])
    assert ap.qtd_faturavel == 1
    assert (ap.qtd_sellers, ap.qtd_bases) == (1, 0)
    assert ap.valor_total == Decimal("0.80")


def test_dual_saida_e_entrada_complementares_usam_preco_de_bases():
    ap = run_dual("coleta_entrada_saida", [
        ev(1, "E1", base="LOJA SELLER", data_entrada=D5, status="na_base"),
        ev(2, "X1", base="LOJA SELLER", status="saiu", data_saida=D6),
    ])
    assert (ap.qtd_sellers, ap.qtd_bases) == (0, 2)
    assert ap.valor_total == Decimal("1.00")


def test_dual_cancelado_sai_pelo_preco_do_lado_e_composicao_fecha():
    ap = run_dual("coleta_saida", [
        ev(1, "S1", base="LOJA SELLER", data_coleta=D5, id_coleta=10, status="coletado"),
        ev(2, "S2", base="LOJA SELLER", data_coleta=D5, id_coleta=10, status="cancelado"),
        ev(3, "B1", base="BASE PARCEIRA", data_coleta=D5, id_coleta=11, status="cancelado"),
    ])
    assert ap.qtd_cancelados == 2 and ap.qtd_faturavel == 1
    assert ap.valor_pacotes == Decimal("0.80")
    comp = {c.classificacao: c.valor for c in ap.composicao}
    assert comp["cancelado"] == Decimal("-1.30")
    assert comp["coleta_identificada"] + comp["cancelado"] == ap.valor_pacotes


def test_dual_coleta_manual_segue_papel_do_cliente():
    ap = run_dual(
        "coleta_saida",
        [],
        manuais=[
            LancamentoManual(id_coleta=20, servico="shopee", base="Loja Seller", data=D5, quantidade=3),
            LancamentoManual(id_coleta=21, servico="avulso", base="Base Parceira", data=D5, quantidade=2),
        ],
    )
    assert (ap.qtd_sellers, ap.qtd_bases) == (3, 2)
    assert ap.valor_pacotes == Decimal("3.40")


def test_preco_unico_continua_igual_sem_modo_dual():
    ap = apurar(
        modelo="coleta_saida", periodo_inicio=INI, periodo_fim=FIM, preco=PRECO_BASES,
        eventos=[ev(1, "S1", base="LOJA SELLER", data_coleta=D5, id_coleta=10, status="coletado")],
    )
    assert ap.dual is False and ap.preco_sellers is None
    assert ap.valor_total == Decimal("0.50")
    assert (ap.qtd_sellers, ap.qtd_bases) == (0, 0)
    assert all(it.papel_preco is None for it in ap.itens)


# ---- hash -------------------------------------------------------------------
def _eventos():
    return [ev(1, "S1", base="LOJA SELLER", data_coleta=D5, id_coleta=10, status="coletado")]


def test_hash_dual_muda_com_preco_de_sellers_e_difere_do_hash_unico():
    base = run_dual("coleta_saida", _eventos())
    outro_preco = apurar(
        modelo="coleta_saida", periodo_inicio=INI, periodo_fim=FIM, preco=PRECO_BASES,
        eventos=_eventos(), dual=True, preco_sellers=Decimal("0.90"), papeis_por_base=PAPEIS,
    )
    unico = apurar(
        modelo="coleta_saida", periodo_inicio=INI, periodo_fim=FIM, preco=PRECO_BASES, eventos=_eventos(),
    )
    assert base.hash != outro_preco.hash
    assert base.hash != unico.hash
    # Determinístico.
    assert base.hash == run_dual("coleta_saida", _eventos()).hash


def test_hash_dual_muda_quando_o_papel_do_cliente_muda():
    seller = run_dual("coleta_saida", _eventos())
    como_base = apurar(
        modelo="coleta_saida", periodo_inicio=INI, periodo_fim=FIM, preco=PRECO_BASES, eventos=_eventos(),
        dual=True, preco_sellers=PRECO_SELLERS, papeis_por_base={"LOJA SELLER": "base_parceira"},
    )
    assert seller.hash != como_base.hash


def test_hash_sem_dual_nao_recebe_campos_novos():
    ap = apurar(
        modelo="coleta_saida", periodo_inicio=INI, periodo_fim=FIM, preco=PRECO_BASES, eventos=_eventos(),
    )
    igual = calcular_hash(
        modelo="coleta_saida", periodo_inicio=INI, periodo_fim=FIM, preco=ap.preco, itens=ap.itens,
        dual=False, preco_sellers=Decimal("9.99"),
    )
    assert igual == ap.hash


def test_preco_faixa_vigente_escolhe_maior_inicio():
    faixas = [
        PrecoFaixa("sellers", date(2026, 11, 1), Decimal("0.10"), fim=None),
        PrecoFaixa("sellers", date(2026, 11, 20), Decimal("0.13"), fim=None),
        PrecoFaixa("bases", date(2026, 10, 1), Decimal("0.05"), fim=None),
    ]
    assert preco_faixa_vigente(faixas, date(2026, 11, 16), "sellers") == Decimal("0.10")
    assert preco_faixa_vigente(faixas, date(2026, 11, 20), "sellers") == Decimal("0.13")
    assert preco_faixa_vigente(faixas, date(2026, 11, 16), "bases") == Decimal("0.05")
    assert preco_faixa_vigente(faixas, date(2026, 9, 1), "sellers") is None


def test_dual_preco_por_data_do_item_mid_period():
    """Sellers só a partir de 20/11: pacotes ≥20 usam 0,13; antes sem faixa → sem_preco."""
    ini, fim = date(2026, 11, 16), date(2026, 11, 30)
    faixas = [
        PrecoFaixa("bases", date(2026, 11, 1), Decimal("0.05"), fim=None),
        PrecoFaixa("sellers", date(2026, 11, 20), Decimal("0.13"), fim=None),
    ]
    ap = apurar(
        modelo="coleta_saida",
        periodo_inicio=ini,
        periodo_fim=fim,
        preco=None,
        eventos=[
            ev(1, "S20", base="LOJA SELLER", data_coleta=date(2026, 11, 20), id_coleta=1, status="coletado"),
            ev(2, "S21", base="LOJA SELLER", data_coleta=date(2026, 11, 21), id_coleta=1, status="coletado"),
            ev(3, "B1", base="BASE PARCEIRA", data_coleta=date(2026, 11, 16), id_coleta=2, status="coletado"),
        ],
        dual=True,
        papeis_por_base=PAPEIS,
        precos_vigencia=faixas,
    )
    assert ap.qtd_sem_preco_sellers == 0
    assert ap.qtd_sem_preco_bases == 0
    assert (ap.qtd_sellers, ap.qtd_bases) == (2, 1)
    assert ap.valor_sellers == Decimal("0.26")
    assert ap.valor_bases == Decimal("0.05")
    assert ap.valor_pacotes == Decimal("0.31")
    assert ap.preco_sellers == Decimal("0.13")
    assert ap.preco == Decimal("0.05")

    com_antes = apurar(
        modelo="coleta_saida",
        periodo_inicio=ini,
        periodo_fim=fim,
        preco=None,
        eventos=[
            ev(1, "S16", base="LOJA SELLER", data_coleta=date(2026, 11, 16), id_coleta=1, status="coletado"),
            ev(2, "S20", base="LOJA SELLER", data_coleta=date(2026, 11, 20), id_coleta=1, status="coletado"),
        ],
        dual=True,
        papeis_por_base=PAPEIS,
        precos_vigencia=faixas,
    )
    assert com_antes.qtd_sem_preco_sellers == 1
    assert com_antes.valor_sellers == Decimal("0.13")  # só o de 20/11


# ---- escopo de preço (service) ----------------------------------------------
def test_preco_vigente_filtra_por_escopo():
    try:
        from owner_cobranca_service import preco_vigente
    except Exception as exc:  # ambiente sem DATABASE_URL/SECRET_KEY
        pytest.skip(f"service indisponível neste ambiente: {exc}")

    def p(escopo, valor, inicio, fim=None):
        return SimpleNamespace(escopo=escopo, valor=Decimal(valor), inicio=inicio, fim=fim)

    precos = [
        p("bases", "0.50", date(2026, 1, 1), date(2026, 10, 31)),
        p("bases", "0.55", date(2026, 11, 1)),
        p("sellers", "0.80", date(2026, 6, 1)),
        SimpleNamespace(valor=Decimal("0.40"), inicio=date(2025, 1, 1), fim=date(2025, 12, 31)),  # legado sem escopo
    ]
    assert preco_vigente(precos, date(2026, 11, 1)).valor == Decimal("0.55")
    assert preco_vigente(precos, date(2026, 11, 1), "sellers").valor == Decimal("0.80")
    assert preco_vigente(precos, date(2026, 5, 1), "sellers") is None
    assert preco_vigente(precos, date(2025, 6, 1)).valor == Decimal("0.40")
