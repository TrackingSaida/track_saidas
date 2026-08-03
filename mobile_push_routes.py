"""
Rotas mobile de push: register/unregister e preferências.
Prefixo: /mobile/push
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_current_user
from db import get_db
from models import DevicePushToken, Motoboy, NotifPrefs, User
from push_notification_service import get_or_create_prefs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mobile/push", tags=["Mobile Push"])


def _require_sub_base(user: User) -> str:
    sub = (getattr(user, "sub_base", None) or "").strip()
    if not sub:
        raise HTTPException(400, "sub_base não definida na sessão.")
    return sub


def _role_int(user: User) -> int:
    raw = getattr(user, "role", None)
    if raw is None or raw == "":
        return 2
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 2


def _resolve_motoboy_id(db: Session, user: User, role: int) -> Optional[int]:
    if role != 4:
        return None
    mid = getattr(user, "motoboy_id", None)
    if mid is not None:
        try:
            return int(mid)
        except (TypeError, ValueError):
            pass
    row = db.scalar(select(Motoboy).where(Motoboy.user_id == user.id))
    return int(row.id_motoboy) if row else None


class RegisterPushIn(BaseModel):
    expo_push_token: str = Field(..., min_length=10, max_length=255)
    platform: Optional[str] = Field(None, max_length=32)


class UnregisterPushIn(BaseModel):
    expo_push_token: str = Field(..., min_length=10, max_length=255)


class NotifPrefsOut(BaseModel):
    fechamento: bool = True
    pacotes_atribuidos: bool = True
    atraso_d1: bool = True
    avisos_base: bool = True
    reconferir_saida: bool = True


class NotifPrefsPatch(BaseModel):
    fechamento: Optional[bool] = None
    pacotes_atribuidos: Optional[bool] = None
    atraso_d1: Optional[bool] = None
    avisos_base: Optional[bool] = None
    reconferir_saida: Optional[bool] = None


@router.post("/register")
def register_push(
    payload: RegisterPushIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _require_sub_base(current_user)
    token = (payload.expo_push_token or "").strip()
    if not token.startswith("ExponentPushToken") and not token.startswith("ExpoPushToken"):
        # Aceita também tokens de dev / formatos futuros, desde que não vazios
        if len(token) < 20:
            raise HTTPException(400, "Token de push inválido.")

    role = _role_int(current_user)
    motoboy_id = _resolve_motoboy_id(db, current_user, role)
    now = datetime.utcnow()

    existing = db.scalar(
        select(DevicePushToken).where(DevicePushToken.expo_push_token == token)
    )
    if existing:
        existing.user_id = current_user.id
        existing.motoboy_id = motoboy_id
        existing.role = role
        existing.sub_base = sub_base
        existing.platform = (payload.platform or existing.platform or "").strip() or None
        existing.ativo = True
        existing.atualizado_em = now
    else:
        db.add(
            DevicePushToken(
                user_id=current_user.id,
                motoboy_id=motoboy_id,
                role=role,
                sub_base=sub_base,
                expo_push_token=token,
                platform=(payload.platform or "").strip() or None,
                ativo=True,
            )
        )

    get_or_create_prefs(
        db,
        user_id=current_user.id,
        sub_base=sub_base,
        motoboy_id=motoboy_id,
    )
    db.commit()
    logger.info(
        "push_register_ok user_id=%s motoboy_id=%s role=%s sub_base=%s platform=%s",
        current_user.id,
        motoboy_id,
        role,
        sub_base,
        (payload.platform or "").strip() or None,
    )
    return {"ok": True, "motoboy_id": motoboy_id}


@router.post("/unregister")
def unregister_push(
    payload: UnregisterPushIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    token = (payload.expo_push_token or "").strip()
    row = db.scalar(
        select(DevicePushToken).where(
            DevicePushToken.expo_push_token == token,
            DevicePushToken.user_id == current_user.id,
        )
    )
    if row:
        row.ativo = False
        row.atualizado_em = datetime.utcnow()
        db.commit()
    return {"ok": True}


def _prefs_out(row: NotifPrefs) -> NotifPrefsOut:
    return NotifPrefsOut(
        fechamento=bool(row.fechamento),
        pacotes_atribuidos=bool(row.pacotes_atribuidos),
        atraso_d1=bool(row.atraso_d1),
        avisos_base=bool(row.avisos_base),
        reconferir_saida=bool(row.reconferir_saida),
    )


@router.get("/preferencias", response_model=NotifPrefsOut)
def get_preferencias(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _require_sub_base(current_user)
    role = _role_int(current_user)
    motoboy_id = getattr(current_user, "motoboy_id", None) if role == 4 else None
    row = get_or_create_prefs(
        db,
        user_id=current_user.id,
        sub_base=sub_base,
        motoboy_id=motoboy_id,
    )
    db.commit()
    return _prefs_out(row)


@router.patch("/preferencias", response_model=NotifPrefsOut)
def patch_preferencias(
    payload: NotifPrefsPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _require_sub_base(current_user)
    role = _role_int(current_user)
    motoboy_id = getattr(current_user, "motoboy_id", None) if role == 4 else None
    row = get_or_create_prefs(
        db,
        user_id=current_user.id,
        sub_base=sub_base,
        motoboy_id=motoboy_id,
    )
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        if value is None:
            continue
        setattr(row, key, bool(value))
    row.atualizado_em = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _prefs_out(row)
