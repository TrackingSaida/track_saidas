from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Integer, Text
from sqlalchemy.orm import Session

from main import Base, get_db

router = APIRouter(prefix="/entregadores", tags=["Entregadores"])

# -----------------------------
# MODELO
# -----------------------------
class Entregador(Base):
    __tablename__ = "entregador"

    id         = Column(Integer, primary_key=True)   # IDENTITY gerado no PostgreSQL
    email_base = Column(Text, nullable=True)         # não tratado pela API
    nome       = Column(Text, nullable=True)
    telefone   = Column(Text, nullable=True)

# -----------------------------
# SCHEMA
# -----------------------------
class EntregadorFields(BaseModel):
    nome: Optional[str] = None
    telefone: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# -----------------------------
# ENDPOINT
# -----------------------------
@router.post("/", status_code=status.HTTP_201_CREATED)
def create_entregador(body: EntregadorFields, db: Session = Depends(get_db)):
    obj = Entregador(
        nome=body.nome,
        telefone=body.telefone,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return {"ok": True, "action": "created", "id": obj.id}
