# entregador_routes.py
from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, BigInteger, Text, Boolean, Date, select
from sqlalchemy.orm import Session

from db import Base, get_db
from auth import get_current_user          # lê o cookie e retorna objeto com id/email/username
from models import User                    # <<< IMPORTA o User já existente (NÃO redeclara!)

router = APIRouter(prefix="/entregadores", tags=["Entregadores"])

# =========================
# MODELO (SQLAlchemy)
# =========================
class Entregador(Base):
    """
    Estrutura alinhada ao banco (conforme suas telas):
      - id_entregador: PK BIGINT
      - ativo: boolean
      - data_cadastro: date
      - base: text (filtro)
      - documento: text
    """
    __tablename__ = "entregador"

    id_entregador = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    nome          = Column(Text, nullable=True)
    telefone      = Column(Text, nullable=True)
    ativo         = Column(Boolean, nullable=False, default=True)
    documento     = Column(Text, nullable=True)
    data_cadastro = Column(Date, nullable=True)
    base          = Column(Text, nullable=False)


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
    (2) Busca na tabela `users` a base do usuário.
    Tenta por id (sub), depois por email/username.
    """
    # 1) por ID
    user_id = getattr(current_user, "id", None)
    if user_id is not None:
        row = db.get(User, user_id)
        if row and row.base:
            return row.base

    # 2) por email
    email = getattr(current_user, "email", None)
    if email:
        u = db.query(User).filter(User.email == email).first()
        if u and u.base:
            return u.base

    # 3) por username
    uname = getattr(current_user, "username", None)
    if uname:
        u = db.query(User).filter(User.username == uname).first()
        if u and u.base:
            return u.base

    raise HTTPException(status_code=400, detail="Base não definida para o usuário em 'users'.")


# =========================
# ROTAS
# =========================
@router.post("/", status_code=status.HTTP_201_CREATED)
def create_entregador(
    body: EntregadorCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    base_user = _resolve_user_base(db, current_user)

    obj = Entregador(
        base=base_user,
        nome=body.nome,
        telefone=body.telefone,
        documento=body.documento,
        ativo=True,  # novo cadastro começa ativo
        # data_cadastro pode ser gerido via DEFAULT/trigger no DB
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
    (2) resolve a base na tabela 'users'
    (3) busca todos os entregadores daquela base
    (4) aplica filtro de status se informado
    """
    base_user = _resolve_user_base(db, current_user)

    stmt = select(Entregador).where(Entregador.base == base_user)

    if status == "ativo":
        stmt = stmt.where(Entregador.ativo.is_(True))
    elif status == "inativo":
        stmt = stmt.where(Entregador.ativo.is_(False))
    # "todos" (ou qualquer outro) => sem filtro adicional

    stmt = stmt.order_by(Entregador.nome)
    rows = db.execute(stmt).scalars().all()
    return rows


@router.get("/{id_entregador}", response_model=EntregadorOut)
def get_entregador(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    base_user = _resolve_user_base(db, current_user)

    obj = db.get(Entregador, id_entregador)
    if not obj or obj.base != base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    return obj
