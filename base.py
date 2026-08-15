# base.py
from __future__ import annotations

from typing import Optional, List, Dict
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, BasePreco, BaseSellerDados  # classe do models.py com __tablename__ = "base"

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
    # Endereço cadastrado em base_seller_dados (opcional; aditivo)
    endereco_completo: Optional[str] = None
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


def _format_endereco_seller(seller: BaseSellerDados) -> Optional[str]:
    """Monta endereço corrido a partir de base_seller_dados (campos vazios são omitidos)."""
    partes: List[str] = []
    if seller.rua:
        rua_num = str(seller.rua).strip()
        if seller.numero:
            rua_num = f"{rua_num}, {str(seller.numero).strip()}"
        partes.append(rua_num)
    if seller.complemento:
        partes.append(str(seller.complemento).strip())
    bairro_cidade: List[str] = []
    if seller.bairro:
        bairro_cidade.append(str(seller.bairro).strip())
    if seller.cidade:
        bairro_cidade.append(str(seller.cidade).strip())
    if bairro_cidade:
        partes.append(" - ".join(bairro_cidade))
    uf_cep: List[str] = []
    if seller.estado:
        uf_cep.append(str(seller.estado).strip())
    if seller.cep:
        uf_cep.append(str(seller.cep).strip())
    if uf_cep:
        partes.append(" ".join(uf_cep))
    texto = ", ".join([p for p in partes if p])
    return texto or None


def _base_to_out(obj: BasePreco, endereco_completo: Optional[str] = None) -> BaseOut:
    return BaseOut(
        id_base=obj.id_base,
        base=obj.base,
        sub_base=obj.sub_base,
        username=obj.username,
        shopee=float(obj.shopee or 0),
        ml=float(obj.ml or 0),
        avulso=float(obj.avulso or 0),
        ativo=bool(obj.ativo),
        endereco_completo=endereco_completo,
    )


def _enderecos_por_base_ids(db: Session, base_ids: List[int]) -> Dict[int, str]:
    if not base_ids:
        return {}
    rows = db.scalars(
        select(BaseSellerDados).where(BaseSellerDados.base_id.in_(base_ids))
    ).all()
    out: Dict[int, str] = {}
    for seller in rows:
        if seller.base_id is None:
            continue
        texto = _format_endereco_seller(seller)
        if texto:
            out[int(seller.base_id)] = texto
    return out

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
        ativo=bool(payload.ativo) if payload.ativo is not None else True,
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
    enderecos = _enderecos_por_base_ids(db, [int(r.id_base) for r in rows])
    return [
        _base_to_out(r, enderecos.get(int(r.id_base)))
        for r in rows
    ]

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
    enderecos = _enderecos_por_base_ids(db, [int(obj.id_base)])
    return _base_to_out(obj, enderecos.get(int(obj.id_base)))

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
    enderecos = _enderecos_por_base_ids(db, [int(obj.id_base)])
    return _base_to_out(obj, enderecos.get(int(obj.id_base)))

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
