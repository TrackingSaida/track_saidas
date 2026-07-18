"""Autorização de digitação manual de códigos."""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Motoboy, User


def normalize_origem_leitura(origem: Optional[str], *, default: str = "camera") -> str:
    value = (origem or default or "camera").strip().lower()
    if value not in ("camera", "manual"):
        return default
    return value


def ensure_manual_code_entry_allowed(
    db: Session,
    user: User,
    *,
    origem: Optional[str],
) -> str:
    """
    Staff (roles 0-3) pode digitar. Motoboy (role 4) só com flag no banco.
    Revalida no DB para permitir revogação sem esperar expirar JWT.
    """
    origem_norm = normalize_origem_leitura(origem)
    role = int(getattr(user, "role", 0) or 0)
    if origem_norm != "manual" or role != 4:
        return origem_norm

    motoboy_id = getattr(user, "motoboy_id", None)
    if not motoboy_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MANUAL_CODE_ENTRY_FORBIDDEN",
                "message": "Digitar código manualmente não é permitido para este perfil.",
            },
        )

    motoboy = db.get(Motoboy, int(motoboy_id))
    if not motoboy or not bool(getattr(motoboy, "pode_digitar_codigo_manual", False)):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MANUAL_CODE_ENTRY_FORBIDDEN",
                "message": "Digitar código manualmente não é permitido para este entregador.",
            },
        )
    return origem_norm
