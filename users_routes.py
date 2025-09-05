from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Integer, Text
from sqlalchemy.orm import Session

from main import Base, get_db

router = APIRouter(prefix="/users", tags=["Users"])

# -----------------------------
# MODELO (id gerado pelo banco)
# -----------------------------
class User(Base):
    __tablename__ = "users"

    id       = Column(Integer, primary_key=True)   # IDENTITY gerado no PostgreSQL
    email    = Column(Text, nullable=True)
    senha    = Column(Text, nullable=True)
    username = Column(Text, nullable=True)
    contato  = Column(Text, nullable=True)

# -----------------------------
# SCHEMA
# -----------------------------
class UserFields(BaseModel):
    email: Optional[str] = None
    senha: Optional[str] = None
    username: Optional[str] = None
    contato: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# -----------------------------
# ENDPOINT
# -----------------------------
@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(body: UserFields, db: Session = Depends(get_db)):
    obj = User(
        email=body.email,
        senha=body.senha,
        username=body.username,
        contato=body.contato,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)  # aqui o banco retorna o id gerado
    return {"ok": True, "action": "created", "id": obj.id}
