"""Ciclo de fechamento da base e hierarquia do PIX a receber (PRD-009)."""
from datetime import date

import pytest

from fechamento_base_ciclo_pure import (
    aviso_ciclo_fechamento,
    ciclo_corresponde,
    normalizar_periodo_fechamento,
    periodo_aberto_para_base,
    renome_fechamento_bloqueado,
    resolver_chave_pix,
)


def test_nenhum_nao_valida_intervalo():
    assert normalizar_periodo_fechamento(None) is None
    assert normalizar_periodo_fechamento("") is None
    assert normalizar_periodo_fechamento(" Nenhum ") is None
    assert ciclo_corresponde(None, date(2026, 10, 1), date(2026, 10, 20))
    assert aviso_ciclo_fechamento("ALVES", None, date(2026, 10, 1), date(2026, 10, 20)) is None


def test_quinzena_civil():
    assert ciclo_corresponde("quinzenal", date(2026, 10, 1), date(2026, 10, 15))
    assert ciclo_corresponde("quinzenal", date(2026, 10, 16), date(2026, 10, 31))
    assert ciclo_corresponde("quinzenal", date(2024, 2, 16), date(2024, 2, 29))
    assert ciclo_corresponde("quinzenal", date(2026, 2, 16), date(2026, 2, 28))
    aviso = aviso_ciclo_fechamento("ALVES BATISTA", "quinzenal", date(2026, 10, 1), date(2026, 10, 20))
    assert aviso is not None
    assert "fecha por quinzena" in aviso
    assert "01/10/2026 a 20/10/2026" in aviso
    assert not ciclo_corresponde("quinzenal", date(2026, 10, 16), date(2026, 11, 15))


def test_semana_segunda_a_domingo_pode_cruzar_o_mes():
    assert ciclo_corresponde("semanal", date(2026, 10, 26), date(2026, 11, 1))
    assert not ciclo_corresponde("semanal", date(2026, 10, 20), date(2026, 10, 26))
    assert not ciclo_corresponde("semanal", date(2026, 10, 5), date(2026, 10, 18))


def test_mes_civil():
    assert ciclo_corresponde("mensal", date(2026, 10, 1), date(2026, 10, 31))
    assert not ciclo_corresponde("mensal", date(2026, 10, 1), date(2026, 11, 30))
    assert not ciclo_corresponde("mensal", date(2026, 10, 1), date(2026, 10, 15))


def test_periodo_invalido():
    with pytest.raises(ValueError):
        normalizar_periodo_fechamento("bimestral")


def test_renome_nao_junta_fechamentos_do_mesmo_periodo():
    outubro = (date(2026, 10, 1), date(2026, 10, 31))
    novembro = (date(2026, 11, 1), date(2026, 11, 30))
    assert renome_fechamento_bloqueado([outubro], [outubro], False)
    assert not renome_fechamento_bloqueado([outubro], [novembro], False)
    assert not renome_fechamento_bloqueado([outubro], [], True)
    assert renome_fechamento_bloqueado([outubro, outubro], [], True)


def test_pix_da_base_prevalece_sobre_o_global():
    assert resolver_chave_pix("base@pix", "global@pix") == "base@pix"
    assert resolver_chave_pix("  ", "global@pix") == "global@pix"
    assert resolver_chave_pix(None, "  ") is None
    assert resolver_chave_pix(None, None) is None


def test_periodo_aberto_respeita_agenda_da_base():
    inicio = date(2026, 11, 9)  # segunda
    fim = date(2026, 11, 15)  # domingo
    sexta = date(2026, 11, 13)
    sabado = date(2026, 11, 14)
    domingo = date(2026, 11, 15)
    # Seg–Sex: na sexta (último dia) não há dia programado depois de hoje.
    assert not periodo_aberto_para_base(inicio, fim, sexta, [1, 2, 3, 4, 5])
    # Seg–Sex: no sábado a agenda do período já encerrou.
    assert not periodo_aberto_para_base(inicio, fim, sabado, [1, 2, 3, 4, 5])
    # Seg–Sáb: na sexta ainda há sábado à frente.
    assert periodo_aberto_para_base(inicio, fim, sexta, [1, 2, 3, 4, 5, 6])
    # Seg–Sáb: no sábado é o último dia; pendência do dia fica no calendário.
    assert not periodo_aberto_para_base(inicio, fim, sabado, [1, 2, 3, 4, 5, 6])
    # Seg–Sáb: no domingo não resta dia programado.
    assert not periodo_aberto_para_base(inicio, fim, domingo, [1, 2, 3, 4, 5, 6])
    # COLETA_EXTRA no domingo mantém aberto no sábado.
    assert periodo_aberto_para_base(inicio, fim, sabado, [1, 2, 3, 4, 5], [domingo])
    # Sem dias_coleta, cai no critério civil.
    assert periodo_aberto_para_base(inicio, fim, sabado, [])
    assert not periodo_aberto_para_base(inicio, fim, date(2026, 11, 16), [])


def test_dias_pendentes_exigem_coleta_encerrada():
    from fechamento_base_ciclo_pure import (
        listar_dias_pendentes_agenda,
        status_coleta_satisfaz_fechamento,
    )

    assert status_coleta_satisfaz_fechamento("coletado")
    assert status_coleta_satisfaz_fechamento("sem_volume")
    assert not status_coleta_satisfaz_fechamento("em_coleta")
    assert not status_coleta_satisfaz_fechamento(None)

    inicio = date(2026, 11, 9)
    fim = date(2026, 11, 13)  # sexta
    dias = [1, 2, 3, 4, 5]
    # Sem execução → pendente.
    assert listar_dias_pendentes_agenda(
        inicio,
        fim,
        dias,
        execucoes_ok=set(),
        lancamentos_legados=set(),
    ) == [
        "2026-11-09",
        "2026-11-10",
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
    ]
    # Só dias com coletado/sem_volume (já filtrados no caller) satisfazem.
    ok = {date(2026, 11, d) for d in (9, 10, 11, 12, 13)}
    assert (
        listar_dias_pendentes_agenda(
            inicio,
            fim,
            dias,
            execucoes_ok=ok,
            lancamentos_legados=set(),
        )
        == []
    )
    # Sexta sem status terminal permanece pendente (em_coleta não entra em execucoes_ok).
    ok_sem_sexta = {date(2026, 11, d) for d in (9, 10, 11, 12)}
    assert listar_dias_pendentes_agenda(
        inicio,
        fim,
        dias,
        execucoes_ok=ok_sem_sexta,
        lancamentos_legados=set(),
    ) == ["2026-11-13"]
