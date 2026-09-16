"""Sanitiza respostas de erro para não expor SQL, stack ou internals ao cliente."""
from __future__ import annotations

from typing import Any

CLIENT_SAFE_500 = "Não foi possível concluir a operação. Tente novamente."

_TECHNICAL_MARKERS = (
    "psycopg",
    "sqlalchemy",
    "undefinedcolumn",
    "undefinedtable",
    "programmingerror",
    "operationalerror",
    "integrityerror",
    "traceback",
    "sqlstate",
    "does not exist",
    "left outer join",
    "inner join",
    "select ",
    "background on this error",
    "sqlalche.me",
    "asyncpg",
    "permission denied for",
    "relation ",
    "column ",
    "line 1:",
    "[sql:",
)


def looks_like_internal_error_dump(value: Any) -> bool:
    text = value if isinstance(value, str) else str(value or "")
    if not text:
        return False
    lower = text.lower()
    if any(marker in lower for marker in _TECHNICAL_MARKERS):
        return True
    if "\n" in text and len(text) > 120:
        return True
    if len(text) > 280:
        return True
    return False


def public_error_body(status: int, detail: Any, *, unhandled: bool = False) -> dict:
    """Corpo JSON seguro para o cliente.

    4xx de negócio permanece intacto.
    500 inesperado ou dump técnico vira mensagem genérica.
    """
    if unhandled or (status >= 500 and looks_like_internal_error_dump(detail)):
        return {"detail": CLIENT_SAFE_500}
    if isinstance(detail, (dict, list)):
        return {"detail": detail}
    text = str(detail).strip() if detail else ""
    if status >= 500:
        return {"detail": text or CLIENT_SAFE_500}
    return {"detail": text or "Erro"}
