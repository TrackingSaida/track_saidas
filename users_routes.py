from __future__ import annotations
from typing import Optional
import logging

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Integer, Text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from main import Base, get_db

router = APIRouter(prefix="/users", tags=["Users"])

# logger dedicado deste módulo
logger = logging.getLogger("routes.users")

class User(Base):
    __tablename__ = "users"
    # ID gerado pelo banco (IDENTITY)
    id       = Column(Integer, primary_key=True)
    # Somente os campos que a API pode tocar
    email    = Column(Text, nullable=True)
    senha    = Column(Text, nullable=True)
    username = Column(Text, nullable=True)
    contato  = Column(Text, nullable=True)

class UserFields(BaseModel):
    email: Optional[str] = None
    senha: Optional[str] = None
    username: Optional[str] = None
    contato: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(body: UserFields, db: Session = Depends(get_db)):
    # log de entrada
    try:
        payload = body.model_dump(exclude_none=False)
    except Exception:
        payload = str(body)
    logger.info("POST /users payload=%s", payload)

    obj = User(
        email=body.email,
        senha=body.senha,
        username=body.username,
        contato=body.contato,
    )
    db.add(obj)
    try:
        db.commit()
        db.refresh(obj)
        logger.info("User criado com sucesso id=%s", obj.id)
        # ==== NÃO ALTERAR FORMATO DE SAÍDA DE SUCESSO ====
        return {"ok": True, "action": "created", "id": obj.id}
    except IntegrityError as e:
        db.rollback()
        logger.exception("IntegrityError ao criar user (talvez UNIQUE/NOT NULL): %s", e)
        # relança para FastAPI gerar 500 (contrato de erro não alterado)
        raise
    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("SQLAlchemyError ao criar user: %s", e)
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Erro inesperado ao criar user: %s", e)
        raise
