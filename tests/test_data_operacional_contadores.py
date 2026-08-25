"""Data operacional: reatribuição em D conta no dia D (não em Saida.data antiga)."""
from datetime import date, datetime

from saida_operacional_pure import SaidaOperacionalContext, janela_timestamp_periodo, timestamp_operacional_saida


def _ctx(
    *,
    operacional_ts: datetime | None,
    ultimo_evento_ts: datetime | None = None,
    removido: bool = False,
) -> SaidaOperacionalContext:
    return SaidaOperacionalContext(
        id_saida=1,
        ultimo_evento="reatribuido",
        ultimo_evento_ts=ultimo_evento_ts or operacional_ts,
        acao_label="Reatribuiu pedido",
        executado_por="op",
        ultimo_ator_username="op",
        ultimo_ator_user_id=1,
        operacional_evento="reatribuido",
        operacional_ts=operacional_ts,
        leitura_valida=True,
        removido_sem_inicio_ativo=removido,
    )


def test_reatribuicao_usa_timestamp_operacional_nao_saida_data():
    """Pacote com Saida.data = D-1 e reatribuição em D → dia operacional = D."""
    d_menos_1 = date(2026, 8, 1)
    d = date(2026, 8, 2)
    saida_ts = datetime(2026, 8, 1, 10, 0, 0)  # criação antiga
    reatrib_ts = datetime(2026, 8, 2, 9, 30, 0)

    ts = timestamp_operacional_saida(_ctx(operacional_ts=reatrib_ts), saida_ts)
    assert ts is not None
    assert ts.date() == d
    assert ts.date() != d_menos_1


def test_sem_evento_operacional_cai_no_timestamp_da_saida():
    saida_ts = datetime(2026, 8, 1, 10, 0, 0)
    ctx = _ctx(operacional_ts=None, ultimo_evento_ts=None)
    ctx.operacional_ts = None
    ctx.ultimo_evento_ts = None
    ts = timestamp_operacional_saida(ctx, saida_ts)
    assert ts == saida_ts


def test_removido_sem_inicio_nao_tem_data_operacional():
    ts = timestamp_operacional_saida(
        _ctx(operacional_ts=datetime(2026, 8, 2, 9, 0, 0), removido=True),
        datetime(2026, 8, 1, 10, 0, 0),
    )
    assert ts is None


def test_janela_timestamp_periodo_e_exclusiva_no_fim():
    inicio, fim_excl = janela_timestamp_periodo(date(2026, 8, 19), date(2026, 8, 19))
    assert inicio == datetime(2026, 8, 19, 0, 0, 0)
    assert fim_excl == datetime(2026, 8, 20, 0, 0, 0)
    assert datetime(2026, 8, 19, 23, 59, 59) >= inicio
    assert datetime(2026, 8, 19, 23, 59, 59) < fim_excl
    assert datetime(2026, 8, 20, 0, 0, 0) >= fim_excl
