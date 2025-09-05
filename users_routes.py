from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ConfigDict

from sqlalchemy import Column, Integer, Text, Date as SA_Date
from sqlalchemy.orm import Session

# usa o mesmo Base e a mesma sessão do seu app
from main import Base, get_db

router = APIRouter(prefix="/users", tags=["Users"])

# ==========================
# MODELO SQLALCHEMY (users)
# ==========================
class User(Base):
    __tablename__ = "users"

    id          = Column(Integer, primary_key=True, autoincrement=False)  # ID informado
    # Pretos (não tratados aqui): email, username, contato, status, cobranca
    senha       = Column(Text, nullable=True)        # << vermelho
    valor       = Column(Text, nullable=True)        # << vermelho (coluna "R$")
    mensalidade = Column(SA_Date, nullable=True)     # << vermelho (data)
    creditos    = Column(Text, nullable=True)        # << vermelho

# ==========================
# SCHEMA (apenas vermelhos)
# ==========================
class UserFields(BaseModel):
    senha: Optional[str] = None
    valor: Optional[str] = None                      # coluna "R$"
    mensalidade: Optional[str] = Field(              # "YYYY-MM-DD" ou "DD/MM/YYYY"
        default=None,
        description='Data em "YYYY-MM-DD" ou "DD/MM/YYYY".'
    )
    creditos: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# ==========================
# Utils
# ==========================
def _parse_date_maybe(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    s = value.strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        pass
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Formato de data inválido para mensalidade: '{value}'. Use 'YYYY-MM-DD' ou 'DD/MM/YYYY'.",
        )

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

    if body.senha is not None:
        obj.senha = body.senha
    if body.valor is not None:
        obj.valor = body.valor
    if body.mensalidade is not None:
        obj.mensalidade = _parse_date_maybe(body.mensalidade)
    if body.creditos is not None:
        obj.creditos = body.creditos

    db.commit()
    db.refresh(obj)
    return {"ok": True, "action": "created" if created else "updated", "id": obj.id}
