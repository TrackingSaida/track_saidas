# entregador_routes.py
from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from datetime import date

from db import get_db
from auth import get_current_user
from models import User, Entregador

router = APIRouter(prefix="/entregadores", tags=["Entregadores"])

# =========================
# SCHEMAS (Pydantic)
# =========================
class EntregadorCreate(BaseModel):
    nome: str
    telefone: str
    documento: str
    model_config = ConfigDict(from_attributes=True)

class EntregadorUpdate(BaseModel):
    # atualização parcial (envie só o que quer alterar)
    nome: Optional[str] = None
    telefone: Optional[str] = None
    documento: Optional[str] = None
    ativo: Optional[bool] = None
    model_config = ConfigDict(from_attributes=True)

class EntregadorOut(BaseModel):
    # Campos exibidos na tabela da página HTML
    id_entregador: int
    nome: Optional[str] = None
    telefone: Optional[str] = None
    documento: Optional[str] = None
    ativo: bool
    data_cadastro: Optional[date] = None
    model_config = ConfigDict(from_attributes=True)

# =========================
# HELPERS
# =========================
def _resolve_user_base(db: Session, current_user) -> str:
    """
    Busca na tabela `users` a sub_base do usuário.
    Tenta por id, depois por email/username.
    """
    # 1) por ID
    user_id = getattr(current_user, "id", None)
    if user_id is not None:
        u = db.get(User, user_id)
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    # 2) por email
    email = getattr(current_user, "email", None)
    if email:
        u = db.scalars(select(User).where(User.email == email)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    # 3) por username
    uname = getattr(current_user, "username", None)
    if uname:
        u = db.scalars(select(User).where(User.username == uname)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    raise HTTPException(status_code=400, detail="sub_base não definida para o usuário em 'users'.")

def _get_owned_entregador(db: Session, sub_base_user: str, id_entregador: int) -> Entregador:
    """
    Passo 2: valida se o entregador existe e pertence à mesma sub_base do solicitante.
    """
    obj = db.get(Entregador, id_entregador)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    return obj

# =========================
# ROTAS
# =========================
@router.post("/", status_code=status.HTTP_201_CREATED)
def create_entregador(
    body: EntregadorCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    sub_base_user = _resolve_user_base(db, current_user)

    obj = Entregador(
        sub_base=sub_base_user,      # grava na coluna sub_base
        nome=(body.nome or "").strip() or None,
        telefone=(body.telefone or "").strip() or None,
        documento=(body.documento or "").strip() or None,
        ativo=True,                  # novo cadastro começa ativo
        # data_cadastro: DEFAULT CURRENT_DATE no banco
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return {"ok": True, "action": "created", "id": obj.id_entregador}

@router.get("/", response_model=List[EntregadorOut])
def list_entregadores(
    status: Optional[str] = Query("todos", description="Filtrar por status: ativo, inativo ou todos"),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    (1) identifica o usuário via cookie
    (2) resolve a sub_base na tabela 'users'
    (3) busca todos os entregadores daquela sub_base
    (4) aplica filtro de status se informado
    """
    sub_base_user = _resolve_user_base(db, current_user)

    stmt = select(Entregador).where(Entregador.sub_base == sub_base_user)

    if status == "ativo":
        stmt = stmt.where(Entregador.ativo.is_(True))
    elif status == "inativo":
        stmt = stmt.where(Entregador.ativo.is_(False))
    # "todos" => sem filtro adicional

    stmt = stmt.order_by(Entregador.nome)
    rows = db.scalars(stmt).all()
    return rows

@router.get("/{id_entregador}", response_model=EntregadorOut)
def get_entregador(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)
    return obj

@router.patch("/{id_entregador}", response_model=EntregadorOut)
def patch_entregador(
    id_entregador: int,
    body: EntregadorUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Edição parcial: altera qualquer combinação de nome/telefone/documento/ativo.
    Passo 1: pega sub_base do solicitante.
    Passo 2: valida se o entregador pertence à mesma sub_base.
    """
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)

    # aplica somente o que veio no body
    if body.nome is not None:
        obj.nome = body.nome.strip()
    if body.telefone is not None:
        obj.telefone = body.telefone.strip()
    if body.documento is not None:
        obj.documento = body.documento.strip()
    if body.ativo is not None:
        obj.ativo = body.ativo

    db.commit()
    db.refresh(obj)
    return obj

@router.delete("/{id_entregador}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entregador(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Passo 3: aplica a mesma validação de base e remove.
    """
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)

    db.delete(obj)
    db.commit()
    return
