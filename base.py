# base.py
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, BasePreco  # << classe do models.py com __tablename__ = "base"

router = APIRouter(prefix="/base", tags=["Base"])

# =========================
# Schemas
# =========================
class BaseCreate(BaseModel):
    base: str = Field(min_length=1)
    shopee: float = Field(ge=0)
    ml: float = Field(ge=0)
    avulso: float = Field(ge=0)
    nfe: float = Field(ge=0)
    model_config = ConfigDict(from_attributes=True)

class BaseOut(BaseModel):
    id_base: int
    base: Optional[str]
    sub_base: Optional[str]
    username: Optional[str]
    shopee: float
    ml: float
    avulso: float
    nfe: float
    model_config = ConfigDict(from_attributes=True)

# =========================
# Helper (igual ao das saídas, mas focado em sub_base)
# =========================
def _resolve_user_sub_base(db: Session, current_user: User) -> str:
    """
    Determina a sub_base do usuário autenticado (sem fallback frouxo).
    Tenta por id, depois email e username.
    Exige 'users.sub_base' preenchido.
    """
    user_id = getattr(current_user, "id", None)
    if user_id is not None:
        u = db.get(User, user_id)
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    email = getattr(current_user, "email", None)
    if email:
        u = db.scalars(select(User).where(User.email == email)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    username = getattr(current_user, "username", None)
    if username:
        u = db.scalars(select(User).where(User.username == username)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    raise HTTPException(status_code=401, detail="Usuário sem 'sub_base' definida em 'users'.")

# =========================
# POST /base  -> cria um registro de preços para a sub_base do usuário
# =========================
@router.post("/", status_code=status.HTTP_201_CREATED)
def criar_precos_base(
    payload: BaseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)

    obj = BasePreco(
        base=(payload.base or "").strip(),
        sub_base=sub_base_user,
        username=getattr(current_user, "username", None),
        shopee=payload.shopee,
        ml=payload.ml,
        avulso=payload.avulso,
        nfe=payload.nfe,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return {"ok": True, "action": "created", "id_base": obj.id_base}
