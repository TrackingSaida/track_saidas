"""Motor da Cobrança de Owners: classificação, deduplicação, manual e cancelamento."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from owner_cobranca_engine import (
    EXPLICACAO_MODELO,
    Ajuste,
    Decisao,
    EventoCodigo,
    LancamentoManual,
    LoteAvulso,
    Remocao,
    apurar,
    filtrar_itens,
    mes_de,
    periodo_anterior,
    periodo_fechavel,
    presets_periodo,
    proximo_periodo,
    quinzena_de,
    situacao_pacotes,
    sugerir_modelo,
    valor_ajuste_assinado,
)

INI = date(2026, 10, 1)
FIM = date(2026, 10, 15)
D5 = date(2026, 10, 5)
D6 = date(2026, 10, 6)
ANTES = date(2026, 9, 28)
DEPOIS = date(2026, 10, 20)
PRECO = Decimal("0.50")


def ev(id_saida, codigo, **kw):
    kw.setdefault("servico", "Shopee")
    kw.setdefault("base", "BASE1")
    return EventoCodigo(id_saida=id_saida, codigo=codigo, **kw)


def run(modelo, eventos, **kw):
    return apurar(
        modelo=modelo,
        periodo_inicio=INI,
        periodo_fim=FIM,
        preco=kw.pop("preco", PRECO),
        eventos=eventos,
        **kw,
    )


def codigos(ap, **filtros):
    return sorted(it.codigo for it in filtrar_itens(ap.itens, **filtros) if it.codigo)


# 1. Coleta + saída: coleta e saída do mesmo código contam uma vez.
def test_coleta_saida_mesmo_codigo_uma_unidade():
    ap = run("coleta_saida", [
        ev(1, "A1", data_coleta=D5, id_coleta=10, status="entregue", data_saida=D6),
    ])
    assert (ap.qtd_bruto, ap.qtd_faturavel) == (1, 1)
    assert ap.itens[0].origem == "coleta"
    assert ap.valor_total == Decimal("0.50")


# 2. Coleta + saída: saída sem coleta fatura como complemento.
def test_coleta_saida_complemento_saida():
    ap = run("coleta_saida", [
        ev(1, "A1", data_coleta=D5, id_coleta=10, status="coletado"),
        ev(2, "B1", status="saiu", data_saida=D6),
    ])
    assert ap.qtd_faturavel == 2
    comp = {c.classificacao: c.quantidade for c in ap.composicao}
    assert comp == {"coleta_identificada": 1, "saida_complementar": 1}


# 3. Entrada + saída: entrada fatura; saída do mesmo código não soma.
def test_entrada_saida_uma_unidade():
    ap = run("entrada_saida", [
        ev(1, "A1", data_entrada=D5, status="entregue", data_saida=D6),
    ])
    assert ap.qtd_faturavel == 1
    assert ap.itens[0].origem == "entrada"


# 4. Entrada + saída: coleta antiga não ocupa a unidade nem bloqueia a entrada.
def test_entrada_saida_ignora_coleta():
    ap = run("entrada_saida", [
        ev(1, "A1", data_coleta=ANTES, id_coleta=9, data_entrada=D5, status="na_base"),
    ])
    assert ap.qtd_faturavel == 1
    assert ap.itens[0].origem == "entrada"
    assert ap.itens[0].data_competencia == D5


# 5. Três fluxos: código repetido em coleta, entrada e saída conta uma vez, como coleta.
def test_tres_fluxos_prioriza_coleta():
    ap = run("coleta_entrada_saida", [
        ev(1, "A1", data_coleta=D5, id_coleta=10, data_entrada=D5, status="entregue", data_saida=D6),
        ev(2, "B1", data_entrada=D5, status="entregue", data_saida=D6),
        ev(3, "C1", status="ausente", data_saida=D6),
    ])
    assert ap.qtd_faturavel == 3
    origens = {it.codigo: it.origem for it in ap.itens}
    assert origens == {"A1": "coleta", "B1": "entrada", "C1": "saida"}


# 6. Somente saída: segunda leitura do mesmo código não soma (duplicidade).
def test_somente_saida_duplicidade():
    ap = run("somente_saida", [
        ev(1, "A1", status="saiu", data_saida=D5),
        ev(2, "a1 ", status="entregue", data_saida=D6),
    ])
    assert ap.qtd_faturavel == 1
    assert ap.qtd_desconsiderados == 1
    dup = filtrar_itens(ap.itens, filtro="duplicado")
    assert len(dup) == 1 and dup[0].id_saida == 2


# 7. Somente saída: coleta e entrada não faturam.
def test_somente_saida_ignora_coleta_e_entrada():
    ap = run("somente_saida", [
        ev(1, "A1", data_coleta=D5, id_coleta=10, status="coletado"),
        ev(2, "B1", data_entrada=D5, status="na_base"),
    ])
    assert ap.qtd_bruto == 0
    assert ap.qtd_desconsiderados == 2
    assert all(it.motivo == "fora_do_modelo" for it in ap.itens)


# 8. Cancelado entra no bruto e sai do faturável uma única vez.
def test_cancelado_deduz_uma_vez():
    ap = run("coleta_saida", [
        ev(1, "A1", data_coleta=D5, id_coleta=10, status="cancelado"),
        ev(2, "B1", data_coleta=D5, id_coleta=10, status="entregue", data_saida=D6),
    ])
    assert (ap.qtd_bruto, ap.qtd_cancelados, ap.qtd_faturavel) == (2, 1, 1)
    assert ap.valor_pacotes == Decimal("0.50")
    comp = {c.classificacao: c.quantidade for c in ap.composicao}
    assert comp["cancelado"] == -1


# 9. Cancelado duplicado não é subtraído duas vezes.
def test_cancelado_duplicado_nao_subtrai_duas_vezes():
    ap = run("somente_saida", [
        ev(1, "A1", status="cancelado", data_saida=D5),
        ev(2, "A1", status="cancelado", data_saida=D6),
    ])
    assert (ap.qtd_bruto, ap.qtd_cancelados, ap.qtd_faturavel) == (1, 1, 0)


# 10. Unidade pertence ao período da origem: coleta anterior + saída agora não fatura aqui.
def test_unidade_de_outro_periodo():
    ap = run("coleta_saida", [
        ev(1, "A1", data_coleta=ANTES, id_coleta=9, status="entregue", data_saida=D5),
    ])
    assert ap.qtd_bruto == 0
    assert ap.itens[0].motivo == "outro_periodo"
    assert any(d.codigo == "outro_periodo" for d in ap.divergencias)


# 11. Movimento só depois do período fica fora sem aparecer.
def test_evento_fora_do_periodo_some():
    ap = run("somente_saida", [ev(1, "A1", status="saiu", data_saida=DEPOIS)])
    assert ap.itens == []
    assert ap.valor_total == Decimal("0.00")


# 12. Coleta manual: saldo sem código, isolado, é faturável e informativo.
def test_manual_isolado_fatura():
    ap = run("coleta_saida", [], manuais=[
        LancamentoManual(id_coleta=50, servico="shopee", base="BASE1", data=D5, quantidade=30),
    ])
    assert ap.qtd_faturavel == 30
    assert ap.valor_total == Decimal("15.00")
    assert [d.codigo for d in ap.divergencias] == ["manual_isolado"]
    assert ap.itens[0].codigo is None and ap.itens[0].quantidade == 30


# 13. Manual + códigos no mesmo dia/base/serviço bloqueia até a decisão.
def test_manual_ambiguo_bloqueia():
    eventos = [ev(1, "A1", data_coleta=D5, id_coleta=10, status="coletado")]
    manual = LancamentoManual(id_coleta=50, servico="shopee", base="base1", data=D5, quantidade=10)
    ap = run("coleta_saida", eventos, manuais=[manual])
    assert [d.codigo for d in ap.bloqueantes] == ["ambiguidade_manual"]
    assert ap.qtd_faturavel == 11

    ok = run("coleta_saida", eventos, manuais=[manual], decisoes=[
        Decisao(tipo="volumes_distintos", id_coleta=50, servico="shopee"),
    ])
    assert ok.bloqueantes == []
    assert ok.qtd_faturavel == 11


# 14. Vínculo: código reconciliado não aumenta o total.
def test_vinculo_reconcilia_sem_somar():
    eventos = [
        ev(1, "A1", data_coleta=D5, id_coleta=10, status="coletado"),
        ev(2, "A2", data_coleta=D5, id_coleta=10, status="coletado"),
    ]
    manual = LancamentoManual(id_coleta=50, servico="shopee", base="BASE1", data=D5, quantidade=10)
    ap = run("coleta_saida", eventos, manuais=[manual], decisoes=[
        Decisao(tipo="vinculo_codigos", id_coleta=50, servico="shopee", codigos=("A1",)),
    ])
    assert ap.bloqueantes == []
    assert ap.qtd_faturavel == 11  # 10 manuais + A2
    assert codigos(ap, filtro="reconciliado") == ["A1"]


# 15. Ajuste de quantidade manual com decisão.
def test_ajuste_quantidade_manual():
    manual = LancamentoManual(id_coleta=50, servico="shopee", base="BASE1", data=D5, quantidade=10)
    ap = run("coleta_saida", [], manuais=[manual], decisoes=[
        Decisao(tipo="ajuste_quantidade", id_coleta=50, servico="shopee", quantidade_ajustada=7),
    ])
    assert ap.qtd_faturavel == 7


# 16. Manual não entra quando o modelo não cobra coleta.
def test_manual_fora_do_modelo():
    manual = LancamentoManual(id_coleta=50, servico="shopee", base="BASE1", data=D5, quantidade=10)
    ap = run("entrada_saida", [], manuais=[manual])
    assert ap.qtd_bruto == 0
    assert ap.divergencias[0].codigo == "manual_fora_do_modelo"


# 17. Avulso identificado: lote não soma por cima dos códigos; lote incompleto avisa.
def test_avulso_nao_dobra_e_lote_incompleto():
    ap = run("coleta_saida", [
        ev(1, "AVULSO-1", servico="Avulso", data_coleta=D5, id_coleta=10, avulso_lote_id=7, status="coletado"),
        ev(2, "AVULSO-2", servico="Avulso", data_coleta=D5, id_coleta=10, avulso_lote_id=7, status="coletado"),
    ], lotes_avulso={7: LoteAvulso(id=7, quantidade_declarada=3, codigos_existentes=2)})
    assert ap.qtd_faturavel == 2
    comp = {c.classificacao: c.quantidade for c in ap.composicao}
    assert comp == {"avulso_adicional": 2}
    assert any(d.codigo == "lote_incompleto" for d in ap.divergencias)


# 18. Código já cobrado em fechamento vigente não entra de novo.
def test_codigo_ja_faturado():
    ap = run("somente_saida", [ev(1, "A1", status="saiu", data_saida=D5)], codigos_ja_faturados=["a1"])
    assert ap.qtd_bruto == 0
    assert ap.itens[0].motivo == "ja_faturado"


# 19. Leitura removida aparece como desconsiderada, sem recriar pacote.
def test_remocao_desconsiderada():
    ap = run("coleta_saida", [], remocoes=[
        Remocao(id_saida=99, codigo="X9", servico="Shopee", base="BASE1", data=D5),
    ])
    assert ap.qtd_bruto == 0
    assert ap.itens[0].motivo == "removido"


# 20. Ajustes em dinheiro não mexem em quantidade.
def test_ajustes_em_dinheiro():
    ap = run("somente_saida", [ev(1, "A1", status="saiu", data_saida=D5)], ajustes=[
        Ajuste(id=1, tipo="acrescimo", valor=Decimal("10")),
        Ajuste(id=2, tipo="credito", valor=Decimal("3")),
    ])
    assert ap.qtd_faturavel == 1
    assert ap.valor_ajustes == Decimal("7.00")
    assert ap.valor_total == Decimal("7.50")


def _dez_pacotes():
    return [ev(i, f"A{i}", status="saiu", data_saida=D5) for i in range(1, 11)]


# 21. Desconto em % do período incide sobre o valor dos pacotes.
def test_desconto_percentual_periodo():
    ap = run("somente_saida", _dez_pacotes(), ajustes=[
        Ajuste(id=1, tipo="desconto", valor=Decimal("0"), escopo="periodo", percentual=Decimal("10")),
    ])
    assert ap.valor_pacotes == Decimal("5.00")
    assert ap.valores_ajustes[1] == Decimal("-0.50")
    assert ap.valor_total == Decimal("4.50")
    comp = {c.classificacao: (c.quantidade, c.valor) for c in ap.composicao}
    assert comp["ajuste_periodo"] == (1, Decimal("-0.50"))


# 22. Isenção zera o total, inclusive com acréscimo e ajuste de ciclo anterior.
def test_isencao_zera_total():
    ap = run("somente_saida", _dez_pacotes(), ajustes=[
        Ajuste(id=1, tipo="acrescimo", valor=Decimal("2"), escopo="ciclo"),
        Ajuste(id=2, tipo="acrescimo", valor=Decimal("1"), escopo="periodo"),
        Ajuste(id=3, tipo="isencao", valor=Decimal("0"), escopo="periodo"),
    ])
    assert ap.valor_total == Decimal("0.00")
    assert ap.valores_ajustes[3] == Decimal("-8.00")
    classes = [c.classificacao for c in ap.composicao]
    assert classes[-3:] == ["ajuste", "ajuste_periodo", "isencao"]
    assert not ap.bloqueantes


# 23. Desconto que deixa o total negativo bloqueia o fechamento.
def test_total_negativo_bloqueia():
    ap = run("somente_saida", _dez_pacotes(), ajustes=[
        Ajuste(id=1, tipo="desconto", valor=Decimal("9"), escopo="periodo"),
    ])
    assert ap.valor_total == Decimal("-4.00")
    assert any(d.codigo == "total_negativo" for d in ap.bloqueantes)


# 23b. Crédito de ciclo anterior maior que o período só avisa (comportamento v1).
def test_credito_ciclo_negativo_so_avisa():
    ap = run("somente_saida", [ev(1, "A1", status="saiu", data_saida=D5)], ajustes=[
        Ajuste(id=1, tipo="credito", valor=Decimal("2")),
    ])
    assert ap.valor_total == Decimal("-1.50")
    assert not ap.bloqueantes
    assert any(d.codigo == "credito_maior_que_periodo" for d in ap.divergencias)


# 24. Ajuste do período muda o hash (conferência precisa ser refeita).
def test_ajuste_periodo_muda_hash():
    sem = run("somente_saida", _dez_pacotes())
    com = run("somente_saida", _dez_pacotes(), ajustes=[
        Ajuste(id=1, tipo="desconto", valor=Decimal("0"), escopo="periodo", percentual=Decimal("5")),
    ])
    assert sem.hash != com.hash


# 25. Situação dos pacotes cobrados: entregues, em entrega, na base e manual.
def test_situacao_pacotes():
    from saidas_routes import normalizar_status_saida

    linhas = [
        ("codigo", "ENTREGUE", 1),
        ("codigo", "entregue", 1),
        ("codigo", "EM_ROTA", 1),
        ("codigo", "ausente", 1),
        ("codigo", "saiu", 1),
        ("codigo", "coletado", 1),
        ("codigo", "NA_BASE", 1),
        ("manual", None, 5),
    ]
    out = {s["chave"]: s["quantidade"] for s in situacao_pacotes(linhas, normalizar_status_saida)}
    assert out == {"entregue": 2, "em_entrega": 3, "na_base": 2, "manual": 5}


def test_explicacao_para_todos_os_modelos():
    from owner_cobranca_engine import MODELOS

    for m in MODELOS:
        assert EXPLICACAO_MODELO.get(m)


def test_valor_ajuste_assinado():
    assert valor_ajuste_assinado("desconto", Decimal("5")) == Decimal("-5")
    assert valor_ajuste_assinado("acrescimo", Decimal("-5")) == Decimal("5")
    assert valor_ajuste_assinado("correcao", Decimal("-2")) == Decimal("-2")


def test_hash_muda_com_operacao_e_preco():
    base = [ev(1, "A1", status="saiu", data_saida=D5)]
    h1 = run("somente_saida", base).hash
    assert run("somente_saida", base).hash == h1
    assert run("somente_saida", base + [ev(2, "B1", status="saiu", data_saida=D5)]).hash != h1
    assert run("somente_saida", base, preco=Decimal("0.60")).hash != h1


def test_pacote_g_nao_muda_preco():
    ap = run("somente_saida", [ev(1, "A1", status="saiu", data_saida=D5, is_grande=True)])
    assert ap.valor_total == Decimal("0.50")
    assert any(d.codigo == "pacote_g" for d in ap.divergencias)


def test_modelo_invalido():
    with pytest.raises(ValueError):
        run("max", [])


def test_sugerir_modelo():
    assert sugerir_modelo(ignorar_coleta=False, entrada_habilitada=False) == "coleta_saida"
    assert sugerir_modelo(ignorar_coleta=True, entrada_habilitada=False) == "somente_saida"
    assert sugerir_modelo(ignorar_coleta=True, entrada_habilitada=True) == "entrada_saida"
    assert sugerir_modelo(ignorar_coleta=False, entrada_habilitada=True) == "coleta_entrada_saida"
    assert (
        sugerir_modelo(ignorar_coleta=False, entrada_habilitada=True, modo_operacao="ambos")
        == "coleta_entrada_saida"
    )
    assert (
        sugerir_modelo(ignorar_coleta=False, entrada_habilitada=True, modo_operacao="coleta_manual")
        == "entrada_saida"
    )
    assert (
        sugerir_modelo(ignorar_coleta=False, entrada_habilitada=False, modo_operacao="coleta_manual")
        == "coleta_saida"
    )


def test_periodos():
    assert quinzena_de(date(2026, 2, 20)) == (date(2026, 2, 16), date(2026, 2, 28))
    assert mes_de(date(2024, 2, 3)) == (date(2024, 2, 1), date(2024, 2, 29))
    assert periodo_fechavel(date(2026, 10, 1), date(2026, 10, 15), "quinzenal")
    assert not periodo_fechavel(date(2026, 10, 1), date(2026, 10, 31), "quinzenal")
    assert periodo_fechavel(date(2026, 10, 1), date(2026, 10, 31), "mensal")
    assert not periodo_fechavel(date(2026, 10, 2), date(2026, 10, 15), "quinzenal")
    assert proximo_periodo(date(2026, 10, 15), "quinzenal") == (date(2026, 10, 16), date(2026, 10, 31))
    assert periodo_anterior(date(2026, 10, 1), "quinzenal") == (date(2026, 9, 16), date(2026, 9, 30))
    ids = [p["id"] for p in presets_periodo(date(2026, 10, 12))]
    assert ids == ["quinzena_atual", "quinzena_anterior", "mes_atual", "mes_anterior"]
    anterior = presets_periodo(date(2026, 1, 5))[1]
    assert (anterior["inicio"], anterior["fim"]) == (date(2025, 12, 16), date(2025, 12, 31))
