from decimal import Decimal

from base_fechamento_status_pure import (
    MOTIVO_DIAS_PENDENTES,
    MOTIVO_JA_GERADO,
    MOTIVO_PACOTES_G,
    MOTIVO_SEM_COLETAS,
    fechamento_base_divergente,
    motivo_bloqueio_lote_base,
)


def _motivo(**kwargs):
    base = {"status_fechamento": None, "qtd_dias": 3, "qtd_dias_pendentes": 0, "total_pacotes_g": 0}
    base.update(kwargs)
    return motivo_bloqueio_lote_base(**base)


def test_base_pendente_sem_bloqueio_pode_gerar():
    assert _motivo() is None


def test_ja_gerado_tem_prioridade():
    assert _motivo(status_fechamento="GERADO", total_pacotes_g=5) == MOTIVO_JA_GERADO
    assert _motivo(status_fechamento="FECHADO") == MOTIVO_JA_GERADO
    assert _motivo(status_fechamento="RECEBIDO") == MOTIVO_JA_GERADO


def test_dias_pendentes_bloqueiam_mesmo_sem_coletas():
    assert _motivo(qtd_dias=0, qtd_dias_pendentes=2) == MOTIVO_DIAS_PENDENTES


def test_sem_coletas():
    assert _motivo(qtd_dias=0) == MOTIVO_SEM_COLETAS


def test_pacotes_g_ficam_fora_do_lote():
    assert _motivo(total_pacotes_g=1) == MOTIVO_PACOTES_G


def test_divergencia_por_valor_ou_g():
    kw = dict(
        bruto_gravado=Decimal("100.00"),
        cancelados_gravado=Decimal("5.00"),
        g_gravado=(1, 0, 0),
        bruto_atual=Decimal("100.00"),
        cancelados_atual=Decimal("5.00"),
        g_atual=(1, 0, 0),
    )
    assert fechamento_base_divergente(**kw) is False
    assert fechamento_base_divergente(**{**kw, "bruto_atual": Decimal("110.00")}) is True
    assert fechamento_base_divergente(**{**kw, "cancelados_atual": Decimal("0.00")}) is True
    assert fechamento_base_divergente(**{**kw, "g_atual": (2, 0, 0)}) is True
