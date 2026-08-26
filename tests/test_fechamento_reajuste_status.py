from entregador_fechamento_status_pure import (
    STATUS_GERADO,
    STATUS_PAGO,
    STATUS_PERMITE_REAJUSTE,
    STATUS_REAJUSTADO,
    status_permite_reajuste,
)


def test_status_permite_reajuste_gerado_e_reajustado():
    assert STATUS_GERADO in STATUS_PERMITE_REAJUSTE
    assert STATUS_REAJUSTADO in STATUS_PERMITE_REAJUSTE
    assert status_permite_reajuste("GERADO") is True
    assert status_permite_reajuste("REAJUSTADO") is True
    assert status_permite_reajuste("gerado") is True


def test_status_fechado_legado_permite_reajuste():
    assert status_permite_reajuste("FECHADO") is True


def test_status_pago_nao_permite_reajuste():
    assert STATUS_PAGO not in STATUS_PERMITE_REAJUSTE
    assert status_permite_reajuste("PAGO") is False
    assert status_permite_reajuste("") is False
    assert status_permite_reajuste(None) is False
