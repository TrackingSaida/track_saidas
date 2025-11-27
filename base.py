# base.py
from __future__ import annotations

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, BasePreco  # classe do models.py com __tablename__ = "base"

router = APIRouter(prefix="/base", tags=["Base"])

# =========================
# Schemas
# =========================
class BaseCreate(BaseModel):
    base: str = Field(min_length=1)
    shopee: float = Field(ge=0)
    ml: float = Field(ge=0)
    avulso: float = Field(ge=0)
    # novo: toggle opcional; se não vier, usamos False (segue server_default)
    ativo: Optional[bool] = None
    model_config = ConfigDict(from_attributes=True)

class BaseOut(BaseModel):
    id_base: int
    base: Optional[str]
    sub_base: Optional[str]
    username: Optional[str]
    shopee: float
    ml: float
    avulso: float
    # novo: expor status
    ativo: bool
    model_config = ConfigDict(from_attributes=True)

class BaseUpdate(BaseModel):
    base: Optional[str] = None
    shopee: Optional[float] = Field(default=None, ge=0)
    ml: Optional[float]     = Field(default=None, ge=0)
    avulso: Optional[float] = Field(default=None, ge=0)
    # novo: permitir alterar status
    ativo: Optional[bool]   = None
    model_config = ConfigDict(from_attributes=True)

# =========================
# Helper
# =========================
def _resolve_user_sub_base(db: Session, current_user: User) -> str:
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
# POST /base
# =========================
@router.post("/", status_code=status.HTTP_201_CREATED)
def criar_precos_base(
    payload: BaseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)

    # Normaliza nome (trim)
    nome = (payload.base or "").strip()
    if not nome:
        raise HTTPException(status_code=400, detail="O campo 'base' não pode ficar vazio.")

    # Verificar duplicidade: mesma base dentro da mesma sub_base
    dup = db.scalars(
        select(BasePreco).where(
            BasePreco.sub_base == sub_base_user,
            BasePreco.base == nome
        )
    ).first()

    if dup:
        raise HTTPException(
            status_code=409,
            detail="Já existe um registro de preços para essa 'base' nesta sub_base."
        )

    # Criar objeto se não houver duplicidade
    obj = BasePreco(
        base=nome,
        sub_base=sub_base_user,
        username=getattr(current_user, "username", None),
        shopee=payload.shopee,
        ml=payload.ml,
        avulso=payload.avulso,
        ativo=bool(payload.ativo) if payload.ativo is not None else False,
    )

    db.add(obj)
    db.commit()
    db.refresh(obj)

    return {"ok": True, "action": "created", "id_base": obj.id_base}

# =========================
# GET /base/
# =========================
@router.get("/", response_model=List[BaseOut])
def list_bases(
    q: Optional[str] = Query(None, description="Filtro por texto em 'base' (contém)"),
    status_flag: Optional[str] = Query(
        "todos",
        alias="status",
        description="Filtrar por status: ativo, inativo ou todos"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)

    stmt = select(BasePreco).where(BasePreco.sub_base == sub_base_user)

    if q:
        stmt = stmt.where(BasePreco.base.ilike(f"%{q.strip()}%"))

    # novo: filtro por ativo/inativo (opcional)
    if status_flag == "ativo":
        stmt = stmt.where(BasePreco.ativo.is_(True))
    elif status_flag in ("inativo", "inativos"):
        stmt = stmt.where(BasePreco.ativo.is_(False))
    # "todos" mantém sem filtro

    stmt = stmt.order_by(BasePreco.base)
    rows = db.scalars(stmt).all()
    return rows

# =========================
# GET /base/{id_base}
# =========================
@router.get("/{id_base}", response_model=BaseOut)
def get_base(
    id_base: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)
    obj = db.get(BasePreco, id_base)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    return obj

# =========================
# PATCH /base/{id_base}
# =========================
@router.patch("/{id_base}", response_model=BaseOut)
def patch_base(
    id_base: int,
    body: BaseUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)
    obj = db.get(BasePreco, id_base)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")

    # Renomear "base" (opcional)
    if body.base is not None:
        new_base = (body.base or "").strip()
        if not new_base:
            raise HTTPException(status_code=400, detail="O campo 'base' não pode ficar vazio.")
        if new_base != obj.base:
            dup = db.scalars(
                select(BasePreco).where(
                    BasePreco.sub_base == sub_base_user,
                    BasePreco.base == new_base,
                    BasePreco.id_base != obj.id_base
                )
            ).first()
            if dup:
                raise HTTPException(
                    status_code=409,
                    detail="Já existe um registro de preços para essa 'base' nesta sub_base."
                )
            obj.base = new_base

    # Atualizações parciais de preço
    if body.shopee is not None:
        obj.shopee = float(body.shopee)
    if body.ml is not None:
        obj.ml = float(body.ml)
    if body.avulso is not None:
        obj.avulso = float(body.avulso)

    # novo: toggle de status
    if body.ativo is not None:
        obj.ativo = bool(body.ativo)

    db.commit()
    db.refresh(obj)
    return obj

# =========================
# DELETE /base/{id_base}
# =========================
@router.delete("/{id_base}", status_code=status.HTTP_204_NO_CONTENT)
def delete_base(
    id_base: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)
    obj = db.get(BasePreco, id_base)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    db.delete(obj)
    db.commit()
    return
