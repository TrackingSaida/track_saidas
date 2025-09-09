# owner_routes.py
from __future__ import annotations
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sqlalchemy import select

from db import get_db
from auth import get_current_user
from models import Owner, User

router = APIRouter(prefix="/owner", tags=["Owner"])

# =========================
# Schemas
# =========================
class OwnerCreate(BaseModel):
    # Se não enviar email/username, usaremos os do usuário autenticado
    email: Optional[str] = None
    username: Optional[str] = None

    # 0 = pré-pago (debita créditos), 1 = mensalidade
    cobranca: Optional[str] = Field(default=None, description="Use '0' ou '1'")
    valor: Optional[float] = None                 # valor unitário (cobrança 0)
    mensalidade: Optional[str] = None             # YYYY-MM-DD
    creditos: Optional[float] = None              # saldo inicial pré-pago
    base: Optional[str] = None
    contato: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class OwnerOut(BaseModel):
    id_owner: int
    email: Optional[str]
    username: Optional[str]
    cobranca: Optional[str]
    valor: Optional[float]
    mensalidade: Optional[date]
    creditos: Optional[float]
    base: Optional[str]
    contato: Optional[str]
    model_config = ConfigDict(from_attributes=True)

# =========================
# Helpers
# =========================
def _parse_date_yyyy_mm_dd(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="mensalidade deve ser YYYY-MM-DD")

# =========================
# Rotas
# =========================
@router.post("/", status_code=status.HTTP_201_CREATED)
def create_owner(
    body: OwnerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Preenche email/username se não vierem no corpo
    email = body.email or getattr(current_user, "email", None)
    username = body.username or getattr(current_user, "username", None)

    if not username:
        raise HTTPException(status_code=400, detail="username não encontrado (envie no corpo ou autentique-se).")

    # (Opcional) evitar duplicar Owner por base
    if body.base:
        exists = db.scalars(select(Owner).where(Owner.base == body.base)).first()
        if exists:
            raise HTTPException(status_code=409, detail="Já existe um Owner para essa base.")

    obj = Owner(
        email=email,
        username=username,
        cobranca=body.cobranca,
        valor=body.valor,
        mensalidade=_parse_date_yyyy_mm_dd(body.mensalidade),
        creditos=body.creditos,
        base=body.base,
        contato=body.contato,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return {"ok": True, "action": "created", "id_owner": obj.id_owner}


@router.get("/me", response_model=OwnerOut)
def get_owner_for_current_user(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Tenta resolver por base, depois por email/username
    base_user = getattr(current_user, "base", None)
    owner = None
    if base_user:
        owner = db.scalars(select(Owner).where(Owner.base == base_user)).first()
    if not owner and current_user.email:
        owner = db.scalars(select(Owner).where(Owner.email == current_user.email)).first()
    if not owner and current_user.username:
        owner = db.scalars(select(Owner).where(Owner.username == current_user.username)).first()

    if not owner:
        raise HTTPException(status_code=404, detail="Owner não encontrado para esse usuário/base.")
    return owner
