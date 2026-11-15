"""Autorização de Registrar Entrada (staff + motoboy)."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Motoboy, MotoboySubBase, Owner, User


def owner_entrada_habilitada(db: Session, sub_base: str, user: User) -> bool:
    if bool(getattr(user, "entrada_obrigatoria_habilitada", False)):
        return True
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    return bool(owner and getattr(owner, "entrada_obrigatoria_habilitada", False))


def require_entrada_access(db: Session, current_user: User) -> str:
    """Staff 0–3 com Controle de entrada ON; motoboy 4 com pode_realizar_entrada no DB."""
    role = int(getattr(current_user, "role", 0) or 0)
    if role not in (0, 1, 2, 3, 4):
        raise HTTPException(status_code=403, detail="Acesso restrito a admin/operador/motoboy.")

    token_sub = (getattr(current_user, "sub_base", None) or "").strip()
    if role == 4:
        motoboy_id = getattr(current_user, "motoboy_id", None)
        if not motoboy_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "ENTRADA_FORBIDDEN",
                    "message": "Registrar entrada não é permitido para este perfil.",
                },
            )
        sub_base = ""
        if token_sub:
            vinculado = db.scalar(
                select(MotoboySubBase.id).where(
                    MotoboySubBase.motoboy_id == int(motoboy_id),
                    MotoboySubBase.sub_base == token_sub,
                    MotoboySubBase.ativo.is_(True),
                )
            )
            if vinculado:
                sub_base = token_sub
        if not sub_base:
            raise HTTPException(status_code=403, detail="Sub-base não definida.")
        motoboy = db.get(Motoboy, int(motoboy_id))
        if not motoboy or not bool(getattr(motoboy, "pode_realizar_entrada", False)):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "ENTRADA_FORBIDDEN",
                    "message": "Registrar entrada não é permitido para este entregador.",
                },
            )
    else:
        sub_base = token_sub
        if not sub_base:
            raise HTTPException(status_code=403, detail="Sub-base não definida.")

    if not owner_entrada_habilitada(db, sub_base, current_user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ENTRADA_DESABILITADA",
                "message": "Registrar entrada não está habilitado para esta base.",
            },
        )
    return sub_base
