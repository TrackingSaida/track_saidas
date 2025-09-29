from __future__ import annotations
from db import get_db
from auth import get_current_user
from models import User, BasePreco
from typing import Optional, List
from fastapi import Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from models import BasePreco, User  # já está importado acima

# =========================
# SCHEMA: atualização parcial
# =========================
class BaseUpdate(BaseModel):
    # por padrão, sugerimos atualizar apenas preços;
    # se quiser permitir renomear a "base", mantenha o campo abaixo.
    base: Optional[str] = None

    shopee: Optional[float] = Field(default=None, ge=0)
    ml: Optional[float]     = Field(default=None, ge=0)
    avulso: Optional[float] = Field(default=None, ge=0)
    nfe: Optional[float]    = Field(default=None, ge=0)
    model_config = ConfigDict(from_attributes=True)

# =========================
# HELPERS (escopo/posse)
# =========================
def _get_owned_basepreco(db: Session, sub_base_user: str, id_base: int) -> BasePreco:
    obj = db.get(BasePreco, id_base)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    return obj

# =========================
# GET /base/  -> lista preços da sub_base do usuário
# =========================
@router.get("/", response_model=List[BaseOut])
def list_bases(
    q: Optional[str] = Query(None, description="Filtro por texto em 'base' (contém)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)

    stmt = select(BasePreco).where(BasePreco.sub_base == sub_base_user)
    if q:
        # filtro simples por LIKE/ILIKE (caso use Postgres, ILIKE é melhor para case-insensitive)
        # Se estiver usando SQLAlchemy 2.x com Postgres, pode usar .ilike
        stmt = stmt.where(BasePreco.base.ilike(f"%{q.strip()}%"))

    stmt = stmt.order_by(BasePreco.base)
    rows = db.scalars(stmt).all()
    return rows

# =========================
# GET /base/{id_base}  -> detalhe
# =========================
@router.get("/{id_base}", response_model=BaseOut)
def get_base(
    id_base: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)
    obj = _get_owned_basepreco(db, sub_base_user, id_base)
    return obj

# =========================
# PATCH /base/{id_base}  -> atualização parcial
# =========================
@router.patch("/{id_base}", response_model=BaseOut)
def patch_base(
    id_base: int,
    body: BaseUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)
    obj = _get_owned_basepreco(db, sub_base_user, id_base)

    # Se permitir renomear a "base", checar duplicidade dentro da mesma sub_base
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

    # Atualizar apenas os campos enviados
    if body.shopee is not None:
        obj.shopee = float(body.shopee)
    if body.ml is not None:
        obj.ml = float(body.ml)
    if body.avulso is not None:
        obj.avulso = float(body.avulso)
    if body.nfe is not None:
        obj.nfe = float(body.nfe)

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
    obj = _get_owned_basepreco(db, sub_base_user, id_base)
    db.delete(obj)
    db.commit()
    return
