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
