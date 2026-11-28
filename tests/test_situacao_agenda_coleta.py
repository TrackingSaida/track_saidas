"""Situação do dia e gate de lançamento respeitam a agenda confirmada da base.

O push noturno usa `_resumo_calendario_fechamento`, que já ignora dias fora de
`dias_coleta`; a regressão aqui cobre a omissão na lista operacional e o
bloqueio de lançamento fora da agenda.
"""
from coleta_agenda_pure import (
    MSG_FORA_AGENDA,
    MSG_SEM_COLETA_CALENDARIO,
    dia_permite_lancamento_coleta,
    omitir_base_situacao_dia,
)


def test_omitir_domingo_com_agenda_seg_sab_confirmada():
    # domingo = ISO 7
    assert omitir_base_situacao_dia(
        agenda_confirmada=True,
        dias_coleta=[1, 2, 3, 4, 5, 6],
        weekday=7,
        tem_execucao=False,
        coleta_extra=False,
    )


def test_nao_omitir_se_agenda_nao_confirmada():
    assert not omitir_base_situacao_dia(
        agenda_confirmada=False,
        dias_coleta=[1, 2, 3, 4, 5, 6],
        weekday=7,
        tem_execucao=False,
        coleta_extra=False,
    )


def test_nao_omitir_dia_programado_ou_com_execucao_ou_extra():
    assert not omitir_base_situacao_dia(
        agenda_confirmada=True,
        dias_coleta=[1, 2, 3, 4, 5, 6],
        weekday=6,
        tem_execucao=False,
        coleta_extra=False,
    )
    assert not omitir_base_situacao_dia(
        agenda_confirmada=True,
        dias_coleta=[1, 2, 3, 4, 5],
        weekday=7,
        tem_execucao=True,
        coleta_extra=False,
    )
    assert not omitir_base_situacao_dia(
        agenda_confirmada=True,
        dias_coleta=[1, 2, 3, 4, 5],
        weekday=7,
        tem_execucao=False,
        coleta_extra=True,
    )


def test_lancamento_bloqueia_domingo_seg_sab():
    ok, msg = dia_permite_lancamento_coleta(
        agenda_confirmada=True,
        dias_coleta=[1, 2, 3, 4, 5, 6],
        weekday=7,
    )
    assert not ok
    assert msg == MSG_FORA_AGENDA


def test_lancamento_bloqueia_sabado_seg_sex():
    ok, msg = dia_permite_lancamento_coleta(
        agenda_confirmada=True,
        dias_coleta=[1, 2, 3, 4, 5],
        weekday=6,
    )
    assert not ok
    assert msg == MSG_FORA_AGENDA


def test_lancamento_permite_sabado_seg_sab():
    ok, msg = dia_permite_lancamento_coleta(
        agenda_confirmada=True,
        dias_coleta=[1, 2, 3, 4, 5, 6],
        weekday=6,
    )
    assert ok and msg is None


def test_lancamento_permite_coleta_extra_no_domingo():
    ok, msg = dia_permite_lancamento_coleta(
        agenda_confirmada=True,
        dias_coleta=[1, 2, 3, 4, 5, 6],
        weekday=7,
        tipo_excecao="COLETA_EXTRA",
    )
    assert ok and msg is None


def test_lancamento_permite_agenda_nao_confirmada():
    ok, msg = dia_permite_lancamento_coleta(
        agenda_confirmada=False,
        dias_coleta=[1, 2, 3, 4, 5],
        weekday=7,
    )
    assert ok and msg is None


def test_lancamento_bloqueia_sem_coleta_no_dia_programado():
    ok, msg = dia_permite_lancamento_coleta(
        agenda_confirmada=True,
        dias_coleta=[1, 2, 3, 4, 5],
        weekday=1,
        tipo_excecao="SEM_COLETA",
    )
    assert not ok
    assert msg == MSG_SEM_COLETA_CALENDARIO


def test_lancamento_permite_feriado_em_dia_programado():
    ok, msg = dia_permite_lancamento_coleta(
        agenda_confirmada=True,
        dias_coleta=[1, 2, 3, 4, 5],
        weekday=1,
        tipo_excecao="FERIADO",
    )
    assert ok and msg is None
