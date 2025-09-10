from __future__ import annotations

from typing import Optional
import logging

from fastapi import APIRouter, Depends, status, HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db import get_db
from auth import get_password_hash
from models import User

router = APIRouter(prefix="/users", tags=["Users"])

# logger dedicado deste módulo
logger = logging.getLogger("routes.users")

# =========================
# Schemas
# =========================
class UserCreate(BaseModel):
    email: EmailStr
    password_hash: str = Field(min_length=4)          # senha em claro; será hasheada
    username: str = Field(min_length=3)
    contato: str
    status: Optional[str] = "ativo"
    base: Optional[str] = None                   # opcional, caso já saiba a base

    model_config = ConfigDict(from_attributes=True)


class UserOut(BaseModel):
    id: int
    email: EmailStr
    username: str
    contato: str
    status: Optional[str] = None
    base: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# =========================
# Rotas
# =========================
@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    # Log seguro (sem senha)
    try:
        payload_log = body.model_dump(exclude={"password_hash"})
        payload_log["password_hash"] = "***"
    except Exception:
        payload_log = "erro ao processar payload"
    logger.info("POST /users payload=%s", payload_log)

    # Unicidade de email
    if body.email:
        exists_email = db.scalars(select(User).where(User.email == body.email)).first()
        if exists_email:
            logger.warning("Tentativa de cadastro com email já existente: %s", body.email)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="endereço de email já existente",
            )

    # Unicidade de username
    if body.username:
        exists_username = db.scalars(select(User).where(User.username == body.username)).first()
        if exists_username:
            logger.warning("Tentativa de cadastro com username já existente: %s", body.username)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="username já existe",
            )

    # Campos obrigatórios (já validados por Pydantic, mas deixo a mensagem clara)
    if not (body.email and body.password_hash and body.username and body.contato):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email, senha, username e contato são obrigatórios",
        )

    # Gera hash e persiste
    try:
        hashed_password = get_password_hash(body.password_hash)

        obj = User(
            email=body.email,
            password_hash=hashed_password,
            username=body.username,
            contato=body.contato,
            status=body.status or "ativo",
            base=body.base,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        logger.info("User criado com sucesso id=%s", obj.id)
        return {"ok": True, "action": "created", "id": obj.id}
    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("SQLAlchemyError ao criar user: %s", e)
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Erro inesperado ao criar user: %s", e)
        raise


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db)):
    """Buscar um usuário por ID (sem dados sensíveis)"""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuário não encontrado",
        )
    return user
