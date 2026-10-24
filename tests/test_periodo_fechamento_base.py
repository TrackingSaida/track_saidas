"""Ciclo de fechamento da base e hierarquia do PIX a receber (PRD-009)."""
from datetime import date

import pytest

from fechamento_base_ciclo_pure import (
    aviso_ciclo_fechamento,
    ciclo_corresponde,
    normalizar_periodo_fechamento,
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
