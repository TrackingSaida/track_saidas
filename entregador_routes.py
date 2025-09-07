# entregador_routes.py
from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, BigInteger, Text, Date, text, select
from sqlalchemy.orm import Session
from datetime import date  # precisa importar

from db import Base, get_db
from auth import get_current_user          # ✅ usa a sua função pronta do auth.py

router = APIRouter(prefix="/entregadores", tags=["Entregadores"])

# ------------------ MODELO (tabela real) ------------------
class Entregador(Base):
    __tablename__ = "entregador"

    id_entregador = Column(BigInteger, primary_key=True, autoincrement=True)
    nome          = Column(Text, nullable=False)
    telefone      = Column(Text, nullable=True)
    status        = Column(Text, nullable=False, server_default=text("'ativo'::text"))
    documento     = Column(Text, nullable=True)
    data_cadastro = Column(Date, nullable=False, server_default=text("CURRENT_DATE"))
    base          = Column(Text, nullable=True)  # pode deixar NOT NULL no banco depois, se quiser

# ------------------ SCHEMAS ------------------
class EntregadorIn(BaseModel):
    nome: str
    telefone: Optional[str] = None
    documento: Optional[str] = None
    status: Optional[str] = None  # se não vier, o banco aplica 'ativo'

class EntregadorOut(BaseModel):
    id_entregador: int
    nome: str
    telefone: Optional[str] = None
    status: str
    documento: Optional[str] = None
    data_cadastro: Optional[date] = None
    base: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

# ------------------ ROTAS ------------------
@router.post("/", response_model=EntregadorOut, status_code=status.HTTP_201_CREATED)
def create_entregador(
    body: EntregadorIn,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Cria um entregador vinculado automaticamente à 'base' do usuário autenticado.
    'status' e 'data_cadastro' podem ficar a cargo dos defaults do banco.
    """
    base_do_usuario = getattr(current_user, "base", None)
    if not base_do_usuario:
        raise HTTPException(status_code=400, detail="Usuário logado não possui 'base' definida.")

    novo = Entregador(
        nome=body.nome,
        telefone=body.telefone,
        documento=body.documento,
        base=base_do_usuario,
        status=body.status if body.status else None,  # None -> deixa o default 'ativo'
    )
    db.add(novo)
    db.commit()
    db.refresh(novo)
    return novo

@router.get("/", response_model=List[EntregadorOut])
def list_entregadores(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Lista somente os entregadores da mesma 'base' do usuário autenticado.
    """
    base_do_usuario = getattr(current_user, "base", None)
    if not base_do_usuario:
        raise HTTPException(status_code=400, detail="Usuário sem 'base' definida.")

    rows = db.execute(
        select(Entregador)
        .where(Entregador.base == base_do_usuario)
        .order_by(Entregador.id_entregador.desc())
    ).scalars().all()
    return rows

@router.get("/{id_entregador}", response_model=EntregadorOut)
def get_entregador(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Retorna um entregador por ID se pertencer à mesma base do usuário.
    """
    obj = db.get(Entregador, id_entregador)
    if not obj:
        raise HTTPException(status_code=404, detail="Entregador não encontrado.")
    if getattr(current_user, "base", None) != obj.base:
        raise HTTPException(status_code=403, detail="Sem acesso a esta base.")
    return obj
