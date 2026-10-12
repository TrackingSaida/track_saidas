"""Regras puras do balanço da contabilidade por status de fechamento."""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional

from base_fechamento_status_pure import (
    STATUS_GERADO as BASE_STATUS_GERADO,
    STATUS_REAJUSTADO as BASE_STATUS_REAJUSTADO,
    STATUS_RECEBIDO,
    normalizar_status_fechamento_base,
)
from entregador_fechamento_status_pure import (
    STATUS_GERADO as ENT_STATUS_GERADO,
    STATUS_PAGO,
    STATUS_REAJUSTADO as ENT_STATUS_REAJUSTADO,
    normalizar_status_fechamento,
)

FAIXA_PENDENTE = "pendente"
FAIXA_GERADO = "gerado"
FAIXA_RECEBIDO = "recebido"
FAIXA_PAGO = "pago"

CENTAVO = Decimal("0.01")


def _d(v) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def faixa_receita(status: Optional[str]) -> Optional[str]:
    """Fechamento de base: GERADO, REAJUSTADO e FECHADO legado -> gerado; RECEBIDO -> recebido."""
    st = normalizar_status_fechamento_base(status)
    if st in (BASE_STATUS_GERADO, BASE_STATUS_REAJUSTADO):
        return FAIXA_GERADO
    if st == STATUS_RECEBIDO:
        return FAIXA_RECEBIDO
    return None


def faixa_despesa(status: Optional[str]) -> Optional[str]:
    """Fechamento de motoboy: GERADO, REAJUSTADO e FECHADO legado -> gerado; PAGO -> pago."""
    st = normalizar_status_fechamento(status)
    if st in (ENT_STATUS_GERADO, ENT_STATUS_REAJUSTADO):
        return FAIXA_GERADO
    if st == STATUS_PAGO:
        return FAIXA_PAGO
    return None


def montar_lado(pendente, gerado, baixado) -> Dict[str, Decimal]:
    """Um lado do balanço (receber ou pagar). `baixado` é recebido ou pago."""
    p = _d(pendente).quantize(CENTAVO)
    g = _d(gerado).quantize(CENTAVO)
    b = _d(baixado).quantize(CENTAVO)
    return {"pendente": p, "gerado": g, "baixado": b, "total": (p + g + b).quantize(CENTAVO)}


def resultado_previsto(receber: Dict[str, Decimal], pagar: Dict[str, Decimal]) -> Decimal:
    """Tudo o que o período deve gerar: (pendente + gerado + recebido) - (pendente + gerado + pago)."""
    return (_d(receber["total"]) - _d(pagar["total"])).quantize(CENTAVO)


def resultado_realizado(receber: Dict[str, Decimal], pagar: Dict[str, Decimal]) -> Decimal:
    """Só o que já foi baixado: recebido - pago."""
    return (_d(receber["baixado"]) - _d(pagar["baixado"])).quantize(CENTAVO)


def variacao_pct(atual, anterior) -> Optional[Decimal]:
    """Variação percentual sobre o valor absoluto anterior; None quando não há base de comparação."""
    a = _d(anterior)
    if a == 0:
        return None
    return ((_d(atual) - a) / abs(a) * Decimal("100")).quantize(CENTAVO)
