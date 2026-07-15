"""Matriz de equivalência — listagem de Registros (data operacional, ordem, totais)."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from saidas_listar_service import (
    MAX_LISTAR_LIMIT,
    SaidaListRow,
    build_operacional_ctx_from_historico_rows,
    clamp_listar_limit,
    filtrar_ordenar_agregar_listagem,
)


def _hist(id_, id_saida, evento, ts, user_id=None):
    return SimpleNamespace(
        id=id_,
        id_saida=id_saida,
        evento=evento,
        timestamp=ts,
        user_id=user_id,
    )


def _row(id_saida, ts, servico="Shopee", entregador="Joao", **kwargs):
    return SaidaListRow(
        id_saida=id_saida,
        timestamp=ts,
        sub_base=kwargs.get("sub_base", "BASE_A"),
        username=kwargs.get("username", "op1"),
        entregador=entregador,
        entregador_id=kwargs.get("entregador_id"),
        motoboy_id=kwargs.get("motoboy_id"),
        codigo=kwargs.get("codigo", f"BR{id_saida:013d}"),
        servico=servico,
        status=kwargs.get("status", "saiu"),
        base=kwargs.get("base", "DRK"),
        is_grande=kwargs.get("is_grande", False),
    )


def test_clamp_listar_limit():
    assert clamp_listar_limit(None) is None
    assert clamp_listar_limit(50) == 50
    assert clamp_listar_limit(9999) == MAX_LISTAR_LIMIT
    assert clamp_listar_limit(-1) == 0


def test_matriz_data_criacao_versus_evento_operacional():
    """Saída antiga com leitura no período D-15 entra pela data operacional."""
    ts_criacao = datetime(2026, 6, 1, 10, 0, 0)
    ts_lido = datetime(2026, 7, 5, 12, 0, 0)
    rows = [_row(1, ts_criacao)]
    historicos = [_hist(10, 1, "lido", ts_lido, user_id=7)]
    ctx = build_operacional_ctx_from_historico_rows([1], historicos, {7: "motoboy1"})
    page, totals, _ = filtrar_ordenar_agregar_listagem(
        rows,
        ctx,
        de=date(2026, 7, 1),
        ate=date(2026, 7, 11),
        limit=50,
        offset=0,
    )
    assert totals["total"] == 1
    assert [r.id_saida for r in page] == [1]
    assert ctx[1].operacional_ts == ts_lido
    assert ctx[1].acao_label == "Leu pedido"
    assert ctx[1].executado_por == "motoboy1"


def test_matriz_removido_sem_inicio_exclui():
    ts = datetime(2026, 7, 5, 10, 0, 0)
    rows = [_row(2, ts)]
    historicos = [
        _hist(1, 2, "lido", datetime(2026, 7, 5, 9, 0, 0)),
        _hist(2, 2, "removido_sem_inicio", datetime(2026, 7, 5, 11, 0, 0)),
    ]
    ctx = build_operacional_ctx_from_historico_rows([2], historicos, {})
    page, totals, _ = filtrar_ordenar_agregar_listagem(
        rows, ctx, de=date(2026, 7, 1), ate=date(2026, 7, 11), limit=50, offset=0
    )
    assert totals["total"] == 0
    assert page == []
    assert ctx[2].removido_sem_inicio_ativo is True


def test_matriz_reatribuicao_apos_remocao_reinclui():
    ts = datetime(2026, 7, 5, 8, 0, 0)
    rows = [_row(3, ts)]
    historicos = [
        _hist(1, 3, "lido", datetime(2026, 7, 5, 9, 0, 0)),
        _hist(2, 3, "removido_sem_inicio", datetime(2026, 7, 5, 10, 0, 0)),
        _hist(3, 3, "lido", datetime(2026, 7, 5, 11, 0, 0), user_id=1),
    ]
    ctx = build_operacional_ctx_from_historico_rows([3], historicos, {1: "ops"})
    page, totals, _ = filtrar_ordenar_agregar_listagem(
        rows, ctx, de=date(2026, 7, 1), ate=date(2026, 7, 11), limit=50, offset=0
    )
    assert totals["total"] == 1
    assert page[0].id_saida == 3
    assert ctx[3].removido_sem_inicio_ativo is False
    assert ctx[3].operacional_ts == datetime(2026, 7, 5, 11, 0, 0)


def test_matriz_empate_timestamp_desempate_por_id_historico():
    """Dois eventos no mesmo timestamp: o de maior id vence como último."""
    ts = datetime(2026, 7, 5, 12, 0, 0)
    historicos = [
        _hist(1, 4, "lido", ts, user_id=1),
        _hist(2, 4, "em_rota", ts, user_id=2),
    ]
    ctx = build_operacional_ctx_from_historico_rows([4], historicos, {1: "a", 2: "b"})
    assert ctx[4].ultimo_evento == "em_rota"
    assert ctx[4].executado_por == "b"
    assert ctx[4].operacional_ts == ts  # lido permanece atribuição


def test_matriz_ordenacao_deterministica_por_id_saida():
    """Mesmo horário operacional: id_saida maior vem primeiro (DESC)."""
    ts = datetime(2026, 7, 5, 12, 0, 0)
    rows = [_row(10, ts, servico="Shopee"), _row(20, ts, servico="Mercado Livre")]
    ctx = build_operacional_ctx_from_historico_rows([10, 20], [], {})
    page, totals, _ = filtrar_ordenar_agregar_listagem(
        rows, ctx, de=date(2026, 7, 1), ate=date(2026, 7, 11), limit=50, offset=0
    )
    assert totals["total"] == 2
    assert [r.id_saida for r in page] == [20, 10]


def test_matriz_paginacao_e_totalizadores_mesmo_conjunto():
    rows = [
        _row(1, datetime(2026, 7, 10, 10, 0, 0), servico="Shopee"),
        _row(2, datetime(2026, 7, 10, 11, 0, 0), servico="Mercado Livre"),
        _row(3, datetime(2026, 7, 10, 12, 0, 0), servico="Avulso"),
        _row(4, datetime(2026, 7, 10, 13, 0, 0), servico="Shopee"),
    ]
    ctx = build_operacional_ctx_from_historico_rows([1, 2, 3, 4], [], {})
    page1, totals1, _ = filtrar_ordenar_agregar_listagem(
        rows, ctx, de=date(2026, 7, 1), ate=date(2026, 7, 11), limit=2, offset=0
    )
    page2, totals2, _ = filtrar_ordenar_agregar_listagem(
        rows, ctx, de=date(2026, 7, 1), ate=date(2026, 7, 11), limit=2, offset=2
    )
    assert totals1 == totals2
    assert totals1["total"] == 4
    assert totals1["sumShopee"] == 2
    assert totals1["sumMercado"] == 1
    assert totals1["sumAvulso"] == 1
    assert [r.id_saida for r in page1] == [4, 3]
    assert [r.id_saida for r in page2] == [2, 1]


def test_matriz_filtro_entregador_e_sub_base_isolada():
    rows = [
        _row(1, datetime(2026, 7, 10, 10, 0, 0), entregador="Ana", sub_base="A"),
        _row(2, datetime(2026, 7, 10, 11, 0, 0), entregador="Bruno", sub_base="A"),
    ]
    ctx = build_operacional_ctx_from_historico_rows([1, 2], [], {})
    page, totals, _ = filtrar_ordenar_agregar_listagem(
        rows,
        ctx,
        de=date(2026, 7, 1),
        ate=date(2026, 7, 11),
        entregador_filter_norm="ana",
        executor_nome_map={1: "Ana", 2: "Bruno"},
        limit=50,
        offset=0,
    )
    assert totals["total"] == 1
    assert page[0].id_saida == 1


def test_matriz_ausencia_historico_usa_timestamp_saida():
    rows = [_row(9, datetime(2026, 7, 8, 15, 0, 0), servico="Shopee")]
    ctx = build_operacional_ctx_from_historico_rows([9], [], {})
    page, totals, _ = filtrar_ordenar_agregar_listagem(
        rows, ctx, de=date(2026, 7, 1), ate=date(2026, 7, 11), limit=10, offset=0
    )
    assert totals["total"] == 1
    assert page[0].id_saida == 9


def test_matriz_fora_do_periodo_excluida():
    rows = [_row(11, datetime(2026, 6, 1, 10, 0, 0))]
    ctx = build_operacional_ctx_from_historico_rows([11], [], {})
    page, totals, _ = filtrar_ordenar_agregar_listagem(
        rows, ctx, de=date(2026, 7, 1), ate=date(2026, 7, 11), limit=10, offset=0
    )
    assert totals["total"] == 0
    assert page == []


def test_matriz_filtro_acao_leu_pedido():
    ts = datetime(2026, 7, 5, 12, 0, 0)
    rows = [_row(12, ts), _row(13, ts)]
    historicos = [
        _hist(1, 12, "lido", ts, user_id=1),
        _hist(2, 13, "entregue", ts, user_id=1),
    ]
    ctx = build_operacional_ctx_from_historico_rows([12, 13], historicos, {1: "u"})
    page, totals, _ = filtrar_ordenar_agregar_listagem(
        rows,
        ctx,
        de=date(2026, 7, 1),
        ate=date(2026, 7, 11),
        acao_tokens=["leu pedido"],
        limit=50,
        offset=0,
    )
    assert totals["total"] == 1
    assert page[0].id_saida == 12
