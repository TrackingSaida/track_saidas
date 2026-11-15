"""Situação do dia respeita a agenda confirmada da base.

O push noturno usa `_resumo_calendario_fechamento`, que já ignora dias fora de
`dias_coleta`; a regressão aqui cobre a omissão na lista operacional.
"""
from coleta_agenda_pure import omitir_base_situacao_dia


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
