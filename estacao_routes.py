from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict

from sqlalchemy import Column, Integer, Text
from sqlalchemy.orm import Session

from main import Base, get_db

router = APIRouter(prefix="/estacoes", tags=["Estacoes"])

# ==========================
# MODELO SQLALCHEMY
# ==========================
class Estacao(Base):
    __tablename__ = "estacao"

    # Preto (não tratado aqui): email_base
    estacao    = Column(Text, nullable=True)      # << vermelho (se quiser int, troque para Integer)

# ==========================
# SCHEMA (apenas vermelho)
# ==========================
class EstacaoFields(BaseModel):
    estacao: Optional[str] = None  # mude para Optional[int] se a coluna for Integer

    model_config = ConfigDict(from_attributes=True)

# ==========================
# ENDPOINT (UPSERT por ID)
# ==========================
@router.post("/{id}", status_code=status.HTTP_200_OK)
def upsert_estacao(id: int, body: EstacaoFields, db: Session = Depends(get_db)):
    obj = db.get(Estacao, id)
    created = False
    if obj is None:
        obj = Estacao(id=id)
        db.add(obj)
        created = True

    if body.estacao is not None:
        obj.estacao = str(body.estacao)

    db.commit()
    db.refresh(obj)
    return {"ok": True, "action": "created" if created else "updated", "id": obj.id}
