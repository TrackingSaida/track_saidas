"""Regras puras da agenda de coleta da base."""
from __future__ import annotations

from typing import Iterable, Optional


def omitir_base_situacao_dia(
    *,
    agenda_confirmada: bool,
    dias_coleta: Optional[Iterable[int]],
    weekday: int,
    tem_execucao: bool,
    coleta_extra: bool,
) -> bool:
    """True quando a base não deve aparecer na situação do dia (fora da agenda)."""
    if tem_execucao:
        return False
    if not agenda_confirmada:
        return False
    if weekday in {int(d) for d in (dias_coleta or [])}:
        return False
    if coleta_extra:
        return False
    return True
