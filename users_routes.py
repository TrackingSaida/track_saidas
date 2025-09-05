from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Integer, Text
from sqlalchemy.orm import Session

# Reaproveita Base e sessão do seu app
from main import Base, get_db

router = APIRouter(prefix="/users", tags=["Users"])

# ==========================
# MODELO SQLALCHEMY (mínimo necessário)
# ==========================
class User(Base):
    __tablename__ = "users"

    id       = Column(Integer, primary_key=True, autoincrement=False)  # ID informado
    email    = Column(Text, nullable=True)     # << vermelho
    senha    = Column(Text, nullable=True)     # << vermelho
    username = Column(Text, nullable=True)     # << vermelho
    contato  = Column(Text, nullable=True)     # << vermelho
    # Demais colunas existentes na tabela (status, cobranca, R$, mensalidade, creditos, etc.)
    # ficam intocadas por este modelo/rota.

# ==========================
# SCHEMA (apenas vermelhos)
# ==========================
class UserFields(BaseModel):
    email: Optional[str] = None
    senha: Optional[str] = None
    username: Optional[str] = None
    contato: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# ==========================
# ENDPOINT (UPSERT por ID)
# ==========================
@router.post("/{id}", status_code=status.HTTP_200_OK)
def upsert_user(id: int, body: UserFields, db: Session = Depends(get_db)):
    obj = db.get(User, id)
    created = False
    if obj is None:
        obj = User(id=id)
        db.add(obj)
        created = True

    if body.email is not None:
        obj.email = body.email
    if body.senha is not None:
        obj.senha = body.senha
    if body.username is not None:
        obj.username = body.username
    if body.contato is not None:
        obj.contato = body.contato

    db.commit()
    db.refresh(obj)
    return {"ok": True, "action": "created" if created else "updated", "id": obj.id}
