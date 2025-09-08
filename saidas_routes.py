from __future__ import annotations

from typing import Optional
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import Column, BigInteger, Text, Date, DateTime, func
from sqlalchemy.orm import Session

# DB e modelos
from db import Base, get_db
from models import User
from auth import get_current_user

router = APIRouter(prefix="/saidas", tags=["Saídas"])

# =========================
# MODELO TABELA SAIDAS
# =========================
class Saida(Base):
    __tablename__ = "saidas"

    id_saida  = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=False), server_default=func.now())
    data      = Column(Date, server_default=func.current_date())

    base      = Column(Text, nullable=True)
    username  = Column(Text, nullable=True)
    entregador= Column(Text, nullable=True)
    codigo    = Column(Text, nullable=True)
    servico   = Column(Text, nullable=True)
    status    = Column(Text, nullable=True)  # sempre "saiu" neste step

# =========================
# SCHEMAS
# =========================
class SaidaCreate(BaseModel):
    entregador: str = Field(min_length=1)
    codigo: str = Field(min_length=1)
    servico: Optional[str] = None

class SaidaOut(BaseModel):
    id_saida: int
    timestamp: datetime
    data: date
    base: Optional[str] = None
    username: Optional[str] = None
    entregador: Optional[str] = None
    codigo: Optional[str] = None
    servico: Optional[str] = None
    status: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# =========================
# ENDPOINT
# =========================
@router.post("/registrar", response_model=SaidaOut, status_code=status.HTTP_201_CREATED)
def registrar_saida(
    payload: SaidaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Primeiro step (MVP) para manipular a tabela 'saidas':
    - Descobre 'base' e 'username' a partir do usuário autenticado (JWT)
    - Grava uma linha em 'saidas' com status='saiu'
    - Sem lógica de cobrança neste step
    """
    base_user = getattr(current_user, "base", None)
    username  = getattr(current_user, "username", None)

    if not base_user or not username:
        raise HTTPException(status_code=401, detail="Usuário sem 'base' ou 'username' configurados.")

    row = Saida(
        base=base_user,
        username=username,
        entregador=payload.entregador,
        codigo=payload.codigo,
        servico=payload.servico or "padrao",
        status="saiu",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
