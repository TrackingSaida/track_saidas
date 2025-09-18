from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user          # lê o cookie e retorna objeto com id/email/username
from models import User, Entregador        # <<< usa os modelos do models.py

router = APIRouter(prefix="/entregadores", tags=["Entregadores"])

# =========================
# SCHEMAS (Pydantic)
# =========================
class EntregadorCreate(BaseModel):
    nome: Optional[str] = None
    telefone: Optional[str] = None
    documento: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class EntregadorOut(BaseModel):
    # Campos exibidos na tabela da página HTML
    id_entregador: int
    nome: Optional[str] = None
    telefone: Optional[str] = None
    documento: Optional[str] = None
    ativo: bool
    model_config = ConfigDict(from_attributes=True)

# =========================
# HELPERS
# =========================
def _resolve_user_base(db: Session, current_user) -> str:
    """
    Busca na tabela `users` a sub_base do usuário (sem fallback).
    Tenta por id (sub), depois por email/username.
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
        sub_base=sub_base_user,   # <<< grava na coluna sub_base
        nome=body.nome,
        telefone=body.telefone,
        documento=body.documento,
        ativo=True,  # novo cadastro começa ativo (override do default do DB)
        # data_cadastro fica a cargo do DEFAULT CURRENT_DATE do banco
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

    obj = db.get(Entregador, id_entregador)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    return obj
