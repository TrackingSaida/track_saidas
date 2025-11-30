from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sqlalchemy import select

from db import get_db
from auth import get_current_user
from models import Owner, User, OwnerCobrancaItem

router = APIRouter(prefix="/owner", tags=["Owner"])

# ============================================================
# SCHEMAS
# ============================================================

class OwnerCreate(BaseModel):
    email: Optional[str] = None
    username: Optional[str] = None
    valor: Optional[float] = Field(default=None)
    sub_base: Optional[str] = None
    contato: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class OwnerUpdate(BaseModel):
    email: Optional[str] = None
    username: Optional[str] = None
    valor: Optional[float] = None
    contato: Optional[str] = None
    ativo: Optional[bool] = None
    ignorar_coleta: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class OwnerOut(BaseModel):
    id_owner: int
    email: Optional[str]
    username: Optional[str]
    valor: Optional[float]
    sub_base: Optional[str]
    contato: Optional[str]
    ativo: bool
    ignorar_coleta: bool

    model_config = ConfigDict(from_attributes=True)


# ============================================================
# HELPERS
# ============================================================

def _get_owner_by_sub_base(db: Session, sub_base: str) -> Optional[Owner]:
    return db.scalar(select(Owner).where(Owner.sub_base == sub_base))


# ============================================================
# CREATE OWNER
# ============================================================

@router.post("/", status_code=201)
def create_owner(
    body: OwnerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    email = body.email or current_user.email
    username = body.username or current_user.username

    if not body.sub_base:
        raise HTTPException(422, "sub_base é obrigatória.")

    exists = db.scalar(select(Owner).where(Owner.sub_base == body.sub_base))
    if exists:
        raise HTTPException(409, "Já existe um Owner para esta sub_base.")

    obj = Owner(
        email=email,
        username=username,
        valor=body.valor or 0.0,
        sub_base=body.sub_base,
        contato=body.contato,
        ativo=True,
        ignorar_coleta=False
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)

    return {"ok": True, "id_owner": obj.id_owner}


# ============================================================
# GET /owner/me
# ============================================================

@router.get("/me", response_model=OwnerOut)
def get_owner_for_current_user(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.sub_base:
        raise HTTPException(404, "Usuário não possui sub_base associada.")

    owner = _get_owner_by_sub_base(db, current_user.sub_base)
    if not owner:
        raise HTTPException(404, "Owner não encontrado para esta sub_base.")

    return owner


# ============================================================
# LISTAR TODOS (ADMIN)
# ============================================================

@router.get("/", response_model=List[OwnerOut])
def list_owners(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != 0:
        raise HTTPException(403, "Acesso restrito ao administrador.")

    return db.scalars(select(Owner)).all()


# ============================================================
# UPDATE (PATCH ÚNICO)
# ============================================================

@router.patch("/{id_owner}", response_model=OwnerOut)
def update_owner(
    id_owner: int,
    body: OwnerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404, "Owner não encontrado.")

    if current_user.role != 0:
        raise HTTPException(403, "Apenas administradores podem editar Owner.")

    # Campos editáveis
    if body.email is not None:
        owner.email = body.email

    if body.username is not None:
        owner.username = body.username

    if body.valor is not None:
        owner.valor = body.valor

    if body.contato is not None:
        owner.contato = body.contato

    # 🔥 Campos adicionados agora
    if body.ativo is not None:
        owner.ativo = body.ativo

    if body.ignorar_coleta is not None:
        owner.ignorar_coleta = body.ignorar_coleta

    db.commit()
    db.refresh(owner)
    return owner


# ============================================================
# ENDPOINTS DE ATIVAR/DESATIVAR (opcionais)
# ============================================================

@router.patch("/{id_owner}/ativar")
def ativar_owner(id_owner: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404)
    if current_user.role != 0:
        raise HTTPException(403)
    owner.ativo = True
    db.commit()
    return {"ok": True}


@router.patch("/{id_owner}/desativar")
def desativar_owner(id_owner: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404)
    if current_user.role != 0:
        raise HTTPException(403)
    owner.ativo = False
    db.commit()
    return {"ok": True}
