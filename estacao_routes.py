from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Integer, Text
from sqlalchemy.orm import Session

from main import Base, get_db

router = APIRouter(prefix="/estacoes", tags=["Estacoes"])

# -----------------------------
# MODELO
# -----------------------------
class Estacao(Base):
    __tablename__ = "estacao"

    id         = Column(Integer, primary_key=True)   # IDENTITY gerado no PostgreSQL
    email_base = Column(Text, nullable=True)         # não tratado pela API
    estacao    = Column(Text, nullable=True)

# -----------------------------
# SCHEMA
# -----------------------------
class EstacaoFields(BaseModel):
    estacao: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# -----------------------------
# ENDPOINT
# -----------------------------
@router.post("/", status_code=status.HTTP_201_CREATED)
def create_estacao(body: EstacaoFields, db: Session = Depends(get_db)):
    obj = Estacao(estacao=body.estacao)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return {"ok": True, "action": "created", "id": obj.id}
