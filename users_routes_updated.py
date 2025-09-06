from __future__ import annotations

from typing import Optional
import logging

from fastapi import APIRouter, Depends, status, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Integer, Text, Numeric, Date
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from db import Base, get_db  # <- agora vem daqui
from auth import get_password_hash


router = APIRouter(prefix="/users", tags=["Users"])

# logger dedicado deste módulo
logger = logging.getLogger("routes.users")


class User(Base):
    __tablename__ = "users"

    # ID gerado pelo banco (IDENTITY)
    id = Column(Integer, primary_key=True)

    # Campos obrigatórios para autenticação
    email = Column(Text, nullable=False, unique=True)
    password_hash = Column(Text, nullable=False)  # Mudança: armazena hash da senha
    username = Column(Text, nullable=False, unique=True)
    contato = Column(Text, nullable=False)
    
    # Campos adicionais conforme a estrutura do banco mostrada na imagem
    status = Column(Text, nullable=True, default='ativo')
    cobranca = Column(Text, nullable=True)
    valor = Column(Numeric(12, 2), nullable=True)
    mensalidade = Column(Date, nullable=True)
    creditos = Column(Numeric(12, 2), nullable=True, default=0.00)


class UserFields(BaseModel):
    email: str
    password: str  # Mudança: recebe senha em texto plano
    username: str
    contato: str
    status: Optional[str] = 'ativo'
    cobranca: Optional[str] = None
    valor: Optional[float] = None
    mensalidade: Optional[str] = None  # Será convertida para Date
    creditos: Optional[float] = 0.00

    model_config = ConfigDict(from_attributes=True)


class UserResponse(BaseModel):
    """Schema para resposta sem dados sensíveis"""
    id: int
    email: str
    username: str
    contato: str
    status: Optional[str] = None
    cobranca: Optional[str] = None
    valor: Optional[float] = None
    mensalidade: Optional[str] = None
    creditos: Optional[float] = None

    model_config = ConfigDict(from_attributes=True)


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(body: UserFields, db: Session = Depends(get_db)):
    # log de entrada
    try:
        # Não loggar a senha por segurança
        payload_log = body.model_dump(exclude_none=False, exclude={'password'})
        payload_log['password'] = '***'
    except Exception:
        payload_log = "erro ao processar payload"
    logger.info("POST /users payload=%s", payload_log)

    # 🔎 Verificar se email já existe
    if body.email:
        existing_email = db.query(User).filter(User.email == body.email).first()
        if existing_email:
            logger.warning("Tentativa de cadastro com email já existente: %s", body.email)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="endereço de email já existente",
            )

    # 🔎 Verificar se username já existe
    if body.username:
        existing_username = db.query(User).filter(User.username == body.username).first()
        if existing_username:
            logger.warning(
                "Tentativa de cadastro com username já existente: %s", body.username
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="username já existe",
            )

    # Validações básicas
    if not body.email or not body.password or not body.username or not body.contato:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email, senha, username e contato são obrigatórios"
        )

    # Converter mensalidade para Date se fornecida
    mensalidade_date = None
    if body.mensalidade:
        try:
            from datetime import datetime
            mensalidade_date = datetime.strptime(body.mensalidade, '%Y-%m-%d').date()
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Formato de data inválido para mensalidade. Use YYYY-MM-DD"
            )

    # Hashear a senha antes de salvar
    hashed_password = get_password_hash(body.password)

    obj = User(
        email=body.email,
        password_hash=hashed_password,  # Mudança: salva hash da senha
        username=body.username,
        contato=body.contato,
        status=body.status or 'ativo',
        cobranca=body.cobranca,
        valor=body.valor,
        mensalidade=mensalidade_date,
        creditos=body.creditos or 0.00,
    )
    db.add(obj)

    try:
        db.commit()
        db.refresh(obj)
        logger.info("User criado com sucesso id=%s", obj.id)

        # ==== NÃO ALTERAR FORMATO DE SAÍDA DE SUCESSO ====
        return {"ok": True, "action": "created", "id": obj.id}

    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("SQLAlchemyError ao criar user: %s", e)
        raise

    except Exception as e:
        db.rollback()
        logger.exception("Erro inesperado ao criar user: %s", e)
        raise


@router.get("/{user_id}", response_model=UserResponse)
def get_user(user_id: int, db: Session = Depends(get_db)):
    """Buscar um usuário por ID (sem dados sensíveis)"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuário não encontrado"
        )
    return user

