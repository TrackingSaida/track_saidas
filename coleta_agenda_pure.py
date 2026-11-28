"""Regras puras da agenda de coleta da base."""
from __future__ import annotations

from typing import Iterable, Optional, Tuple

MSG_FORA_AGENDA = (
    "Este dia está fora da agenda de coleta da base. "
    "Se precisar coletar, cadastre Coleta extra no calendário."
)
MSG_SEM_COLETA_CALENDARIO = (
    "Este dia está marcado como sem coleta ou justificado no calendário."
)


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


def dia_permite_lancamento_coleta(
    *,
    agenda_confirmada: bool,
    dias_coleta: Optional[Iterable[int]],
    weekday: int,
    tipo_excecao: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Indica se a data operacional aceita lançamento de coleta.

    Retorna ``(True, None)`` quando permitido, ou ``(False, mensagem)`` para 422.
    """
    if not agenda_confirmada:
        return True, None
    tipo = (tipo_excecao or "").strip().upper() or None
    if tipo in ("SEM_COLETA", "JUSTIFICADO"):
        return False, MSG_SEM_COLETA_CALENDARIO
    if tipo == "COLETA_EXTRA":
        return True, None
    dias = {int(d) for d in (dias_coleta or [])}
    if weekday in dias:
        return True, None
    return False, MSG_FORA_AGENDA
