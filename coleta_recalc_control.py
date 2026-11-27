"""Controle pontual para adiar recalcular_coleta em updates em massa (ex.: finalizar-lote)."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_skip_recalcular_coleta: ContextVar[bool] = ContextVar(
    "skip_recalcular_coleta", default=False
)


def should_skip_recalcular_coleta() -> bool:
    return bool(_skip_recalcular_coleta.get())


@contextmanager
def defer_recalcular_coleta() -> Iterator[None]:
    """Durante o bloco, saida_after_update não chama recalcular_coleta."""
    token = _skip_recalcular_coleta.set(True)
    try:
        yield
    finally:
        _skip_recalcular_coleta.reset(token)
