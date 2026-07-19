from datetime import date, datetime

from fechamento_criterio_pure import (
    HistoricoEventoLite,
    classificar_preview_entrega,
    filtrar_entregas_no_periodo,
    resolver_entrega_efetiva,
)


def _ev(evento, dia, hora=12, mid_novo=None, id_=1):
    return HistoricoEventoLite(
        id=id_,
        evento=evento,
        timestamp=datetime(2026, 6, dia, hora, 0, 0),
        motoboy_id_novo=mid_novo,
    )


def test_bipado_dia_30_entregue_dia_01_cai_na_proxima_quinzena():
    eventos = [
        _ev("scan", 30, mid_novo=10, id_=1),
        HistoricoEventoLite(
            id=2,
            evento="entregue",
            timestamp=datetime(2026, 7, 1, 9, 0, 0),
            motoboy_id_novo=None,
        ),
    ]
    entrega = resolver_entrega_efetiva(1, eventos, motoboy_atual=10)
    assert entrega is not None
    assert entrega.data_confirmacao == date(2026, 7, 1)
    assert entrega.motoboy_id == 10
    assert entrega.data_operacional == date(2026, 6, 30)

    q1 = filtrar_entregas_no_periodo([entrega], date(2026, 6, 16), date(2026, 6, 30), motoboy_id=10)
    q2 = filtrar_entregas_no_periodo([entrega], date(2026, 7, 1), date(2026, 7, 15), motoboy_id=10)
    assert q1 == []
    assert len(q2) == 1


def test_bipado_dia_15_entregue_dia_16_cai_na_proxima_quinzena():
    eventos = [
        _ev("lido", 15, mid_novo=7, id_=1),
        HistoricoEventoLite(
            id=2,
            evento="entregue",
            timestamp=datetime(2026, 6, 16, 8, 0, 0),
            motoboy_id_novo=None,
        ),
    ]
    entrega = resolver_entrega_efetiva(2, eventos, motoboy_atual=7)
    assert entrega.data_confirmacao == date(2026, 6, 16)
    assert filtrar_entregas_no_periodo([entrega], date(2026, 6, 1), date(2026, 6, 15)) == []
    assert len(filtrar_entregas_no_periodo([entrega], date(2026, 6, 16), date(2026, 6, 30))) == 1


def test_reatribuicao_apos_entrega_invalida_ate_nova_entrega():
    eventos = [
        _ev("scan", 10, mid_novo=1, id_=1),
        _ev("entregue", 11, id_=2),
        _ev("reatribuicao", 12, mid_novo=2, id_=3),
    ]
    entrega = resolver_entrega_efetiva(3, eventos, motoboy_atual=2)
    assert entrega is not None
    assert entrega.reaberta is True
    assert classificar_preview_entrega(
        entrega, periodo_inicio=date(2026, 6, 1), periodo_fim=date(2026, 6, 15)
    ) == "reaberto"

    eventos.append(_ev("entregue", 13, id_=4))
    entrega2 = resolver_entrega_efetiva(3, eventos, motoboy_atual=2)
    assert entrega2 is not None
    assert entrega2.reaberta is False
    assert entrega2.motoboy_id == 2
    assert entrega2.data_confirmacao == date(2026, 6, 13)


def test_ausente_nao_gera_entrega_efetiva():
    eventos = [
        _ev("scan", 5, mid_novo=3, id_=1),
        _ev("ausente", 6, id_=2),
    ]
    entrega = resolver_entrega_efetiva(4, eventos, motoboy_atual=3)
    assert entrega is None
    assert classificar_preview_entrega(
        None,
        periodo_inicio=date(2026, 6, 1),
        periodo_fim=date(2026, 6, 15),
        status_atual="AUSENTE",
    ) == "ausente"


def test_motoboy_pago_e_da_atribuicao_anterior_a_entrega():
    eventos = [
        _ev("scan", 1, mid_novo=11, id_=1),
        _ev("reatribuicao", 2, mid_novo=22, id_=2),
        _ev("entregue", 3, id_=3),
    ]
    entrega = resolver_entrega_efetiva(5, eventos, motoboy_atual=22)
    assert entrega.motoboy_id == 22
