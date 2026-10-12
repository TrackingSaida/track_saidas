"""Regras puras de status do fechamento de base (coletas)."""
from __future__ import annotations

from typing import Optional

STATUS_GERADO = "GERADO"
STATUS_REAJUSTADO = "REAJUSTADO"
STATUS_RECEBIDO = "RECEBIDO"
STATUS_PERMITE_REAJUSTE = (STATUS_GERADO, STATUS_REAJUSTADO)


def normalizar_status_fechamento_base(status: Optional[str]) -> str:
    st = (status or "").strip().upper()
    if st == "FECHADO":
        return STATUS_GERADO
    return st


def status_base_permite_reajuste(status: Optional[str]) -> bool:
    """GERADO, REAJUSTADO e o legado FECHADO podem ser reajustados. RECEBIDO não."""
    return normalizar_status_fechamento_base(status) in STATUS_PERMITE_REAJUSTE


def status_base_bloqueia_edicao_coleta(status: Optional[str]) -> bool:
    """Coletas de período com fechamento já recebido não podem mais ser alteradas."""
    return normalizar_status_fechamento_base(status) == STATUS_RECEBIDO


MOTIVO_JA_GERADO = "JA_GERADO"
MOTIVO_SEM_COLETAS = "SEM_COLETAS"
MOTIVO_DIAS_PENDENTES = "DIAS_PENDENTES"
MOTIVO_PACOTES_G = "PACOTES_G"


def motivo_bloqueio_lote_base(
    *,
    status_fechamento: Optional[str],
    qtd_dias: int,
    qtd_dias_pendentes: int,
    total_pacotes_g: int,
) -> Optional[str]:
    """
    Motivo pelo qual a base não pode ser gerada no lote (None = pode gerar).
    Pacotes G exigem valor por pacote informado manualmente, então ficam fora do lote.
    """
    if normalizar_status_fechamento_base(status_fechamento):
        return MOTIVO_JA_GERADO
    if qtd_dias_pendentes > 0:
        return MOTIVO_DIAS_PENDENTES
    if qtd_dias <= 0:
        return MOTIVO_SEM_COLETAS
    if total_pacotes_g > 0:
        return MOTIVO_PACOTES_G
    return None


def fechamento_base_divergente(
    *,
    bruto_gravado,
    cancelados_gravado,
    g_gravado: tuple,
    bruto_atual,
    cancelados_atual,
    g_atual: tuple,
) -> bool:
    """Coletas, cancelados ou pacotes G mudaram depois de gerar o fechamento."""
    return (
        bruto_atual != bruto_gravado
        or cancelados_atual != cancelados_gravado
        or tuple(g_atual) != tuple(g_gravado)
    )
