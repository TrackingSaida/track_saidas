from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict

from sqlalchemy import Column, Integer, Text
from sqlalchemy.orm import Session

from main import Base, get_db

router = APIRouter(prefix="/entregadores", tags=["Entregadores"])

# ==========================
# MODELO SQLALCHEMY
# ==========================
class Entregador(Base):
    __tablename__ = "entregador"

    # Preto (não tratado aqui): email_base
    nome       = Column(Text, nullable=True)      # << vermelho
    telefone   = Column(Text, nullable=True)      # << vermelho

# ==========================
# SCHEMA (apenas vermelhos)
# ==========================
class EntregadorFields(BaseModel):
    nome: Optional[str] = None
    telefone: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# ==========================
# ENDPOINT (UPSERT por ID)
# ==========================
@router.post("/{id}", status_code=status.HTTP_200_OK)
def upsert_entregador(id: int, body: EntregadorFields, db: Session = Depends(get_db)):
    obj = db.get(Entregador, id)
    created = False
    if obj is None:
        obj = Entregador(id=id)
        db.add(obj)
        created = True

    if body.nome is not None:
        obj.nome = body.nome
    if body.telefone is not None:
        obj.telefone = body.telefone

    db.commit()
    db.refresh(obj)
    return {"ok": True, "action": "created" if created else "updated", "id": obj.id}
