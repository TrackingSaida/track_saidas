"""Marcos leves do fechamento batem com a regra de data operacional."""
from datetime import datetime

from saida_operacional_pure import (
    EVENTOS_ATRIBUICAO_VALIDOS,
    EVENTOS_INVALIDANTES,
    SaidaOperacionalContext,
    marcos_de_ultimos_eventos,
    resolver_marcos_operacionais_fechamento,
    timestamp_operacional_marcos,
    timestamp_operacional_saida,
)


def _ts(dia: int, hora: int = 10) -> datetime:
    return datetime(2026, 9, dia, hora, 0, 0)


def _ctx_de_marcos(marcos, saida_id: int = 1) -> SaidaOperacionalContext:
    return SaidaOperacionalContext(
        id_saida=saida_id,
        ultimo_evento=None,
        ultimo_evento_ts=marcos.ultimo_evento_ts,
        acao_label=None,
        executado_por=None,
        ultimo_ator_username=None,
        ultimo_ator_user_id=None,
        operacional_evento=None,
        operacional_ts=marcos.operacional_ts,
        leitura_valida=marcos.operacional_ts is not None,
        removido_sem_inicio_ativo=marcos.removido_sem_inicio_ativo,
    )


def test_atribuicao_em_dia_diferente_do_timestamp_da_saida():
    saida_ts = _ts(16)
    atribuicao = _ts(20, 9)
    marcos = resolver_marcos_operacionais_fechamento([("scan", atribuicao)])
    assert timestamp_operacional_marcos(marcos, saida_ts).date().isoformat() == "2026-09-20"
    assert timestamp_operacional_saida(_ctx_de_marcos(marcos), saida_ts) == atribuicao


def test_remocao_depois_da_atribuicao_exclui_saida():
    saida_ts = _ts(16)
    marcos = resolver_marcos_operacionais_fechamento(
        [
            ("scan", _ts(16, 8)),
            ("removido_sem_inicio", _ts(17, 11)),
        ]
    )
    assert marcos.removido_sem_inicio_ativo is True
    assert timestamp_operacional_marcos(marcos, saida_ts) is None
    assert timestamp_operacional_saida(_ctx_de_marcos(marcos), saida_ts) is None


def test_nova_atribuicao_depois_da_remocao_volta_a_contar():
    saida_ts = _ts(16)
    nova = _ts(18, 14)
    marcos = resolver_marcos_operacionais_fechamento(
        [
            ("scan", _ts(16, 8)),
            ("desatribuido", _ts(17, 11)),
            ("reatribuicao", nova),
        ]
    )
    assert marcos.removido_sem_inicio_ativo is False
    assert timestamp_operacional_marcos(marcos, saida_ts) == nova
    assert timestamp_operacional_saida(_ctx_de_marcos(marcos), saida_ts) == nova


def test_sem_atribuicao_usa_ultimo_evento_de_tela():
    saida_ts = _ts(16)
    entregue = _ts(19, 18)
    marcos = resolver_marcos_operacionais_fechamento([("entregue", entregue)])
    assert marcos.operacional_ts is None
    assert marcos.ultimo_evento_ts == entregue
    assert timestamp_operacional_marcos(marcos, saida_ts) == entregue
    assert timestamp_operacional_saida(_ctx_de_marcos(marcos), saida_ts) == entregue


def test_sem_eventos_cai_no_timestamp_da_saida():
    saida_ts = _ts(16)
    marcos = resolver_marcos_operacionais_fechamento([])
    assert timestamp_operacional_marcos(marcos, saida_ts) == saida_ts
    assert timestamp_operacional_saida(_ctx_de_marcos(marcos), saida_ts) == saida_ts


def _ultimos_de_stream(eventos):
    """Simula DISTINCT ON: último atrib/inv e último marco do stream ordenado."""
    ult_ai_ev = ult_ai_ts = ult_marco_ts = None
    for ev, ts in eventos:
        evento = (ev or "").strip().lower()
        ult_marco_ts = ts
        if evento in EVENTOS_ATRIBUICAO_VALIDOS or evento in EVENTOS_INVALIDANTES:
            ult_ai_ev, ult_ai_ts = evento, ts
    return marcos_de_ultimos_eventos(
        ultimo_atrib_ou_inv_evento=ult_ai_ev,
        ultimo_atrib_ou_inv_ts=ult_ai_ts,
        ultimo_marco_ts=ult_marco_ts,
    )


def test_distinct_on_equivale_ao_resolver_completo():
    casos = [
        [("scan", _ts(20, 9))],
        [("scan", _ts(16, 8)), ("removido_sem_inicio", _ts(17, 11))],
        [("scan", _ts(16, 8)), ("desatribuido", _ts(17, 11)), ("reatribuicao", _ts(18, 14))],
        [("entregue", _ts(19, 18))],
        [("scan", _ts(16, 8)), ("entregue", _ts(19, 18))],
        [],
    ]
    for eventos in casos:
        completo = resolver_marcos_operacionais_fechamento(eventos)
        resumido = _ultimos_de_stream(eventos)
        assert resumido == completo, eventos
