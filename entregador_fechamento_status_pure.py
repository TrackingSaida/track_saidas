"""Regras puras de status do fechamento de entregador."""
from __future__ import annotations

from typing import Optional

STATUS_GERADO = "GERADO"
STATUS_REAJUSTADO = "REAJUSTADO"
STATUS_PAGO = "PAGO"
STATUS_ELEGIVEIS_PAGAMENTO = (STATUS_GERADO, STATUS_REAJUSTADO)
STATUS_PERMITE_REAJUSTE = (STATUS_GERADO, STATUS_REAJUSTADO)


def normalizar_status_fechamento(status: Optional[str]) -> str:
    st = (status or "").strip().upper()
    if st == "FECHADO":
        return STATUS_GERADO
    return st


def status_permite_reajuste(status: Optional[str]) -> bool:
    """GERADO, REAJUSTADO e o legado FECHADO podem ser reajustados. PAGO não."""
    return normalizar_status_fechamento(status) in STATUS_PERMITE_REAJUSTE
