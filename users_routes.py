from __future__ import annotations
from typing import Optional
import logging

from fastapi import APIRouter, Depends, status, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Integer, Text
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from passlib.context import CryptContext  # <-- NEW
from main import Base, get_db

router = APIRouter(prefix="/users", tags=["Users"])
logger = logging.getLogger("routes.users")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")  # <-- NEW

class User(Base):
    __tablename__ = "users"
    id            = Column(Integer, primary_key=True)
    email         = Column(Text, nullable=True, unique=True)
    senha         = Column(Text, nullable=True)           # legado (plain text) — NÃO usar em login
    username      = Column(Text, nullable=True, unique=True)
    contato       = Column(Text, nullable=True)
    password_hash = Column(Text, nullable=True)           # <-- NEW (usar no login)

class UserFields(BaseModel):
    email: Optional[str] = None
    senha: Optional[str] = None  # continua recebendo no payload
    username: Optional[str] = None
    contato: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(body: UserFields, db: Session = Depends(get_db)):
    try:
        payload = body.model_dump(exclude_none=False)
    except Exception:
        payload = str(body)
    logger.info("POST /users payload=%s", payload)

    # unicidade
    if body.email:
        if db.query(User).filter(User.email == body.email).first():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="endereço de email já existente")
    if body.username:
        if db.query(User).filter(User.username == body.username).first():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username já existe")

    # gerar hash se veio 'senha'
    password_hash = None
    if body.senha:
        password_hash = pwd_context.hash(body.senha)

    obj = User(
        email=body.email,
        senha=body.senha,              # legado (pode remover depois)
        username=body.username,
        contato=body.contato,
        password_hash=password_hash,   # <-- grava o hash
    )

    db.add(obj)
    try:
        db.commit()
        db.refresh(obj)
        return {"ok": True, "action": "created", "id": obj.id}
    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("SQLAlchemyError ao criar user: %s", e)
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Erro inesperado ao criar user: %s", e)
        raise
