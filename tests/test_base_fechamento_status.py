from base_fechamento_status_pure import (
    STATUS_GERADO,
    STATUS_PERMITE_REAJUSTE,
    STATUS_REAJUSTADO,
    STATUS_RECEBIDO,
    status_base_bloqueia_edicao_coleta,
    status_base_permite_reajuste,
)


def test_base_gerado_e_reajustado_permitem_reajuste():
    assert STATUS_GERADO in STATUS_PERMITE_REAJUSTE
    assert STATUS_REAJUSTADO in STATUS_PERMITE_REAJUSTE
    assert status_base_permite_reajuste("GERADO") is True
    assert status_base_permite_reajuste("reajustado") is True
    assert status_base_permite_reajuste("FECHADO") is True


def test_base_recebido_nao_permite_reajuste():
    assert STATUS_RECEBIDO not in STATUS_PERMITE_REAJUSTE
    assert status_base_permite_reajuste("RECEBIDO") is False
    assert status_base_permite_reajuste("") is False
    assert status_base_permite_reajuste(None) is False


def test_edicao_coleta_bloqueada_somente_quando_recebido():
    assert status_base_bloqueia_edicao_coleta("RECEBIDO") is True
    assert status_base_bloqueia_edicao_coleta("recebido") is True
    assert status_base_bloqueia_edicao_coleta("GERADO") is False
    assert status_base_bloqueia_edicao_coleta("REAJUSTADO") is False
    assert status_base_bloqueia_edicao_coleta("FECHADO") is False
    assert status_base_bloqueia_edicao_coleta(None) is False
