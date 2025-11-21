from __future__ import annotations

from typing import Optional
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db import get_db
from auth import get_current_user, get_password_hash, verify_password
from models import User

router = APIRouter(prefix="/users", tags=["Users"])

# logger dedicado deste módulo
logger = logging.getLogger("routes.users")


# =========================
# Schemas para criação e leitura básica de usuário
# =========================
class UserCreate(BaseModel):
    email: EmailStr
    password_hash: str = Field(min_length=4, description="Senha em claro; será hasheada")
    username: str = Field(min_length=3)
    contato: str

    # Novos campos opcionais
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    status: Optional[bool] = True
    sub_base: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class UserOut(BaseModel):
    id: int
    email: EmailStr
    username: str
    contato: str

    status: Optional[bool] = None
    sub_base: Optional[str] = None
    nome: Optional[str] = None
    sobrenome: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# =========================
# Schemas adicionais para perfil/alteração de senha
# =========================
class UserFull(BaseModel):
    """Schema completo de um usuário para leitura (inclui status)."""
    id: int
    email: Optional[EmailStr] = None
    username: Optional[str] = None
    contato: Optional[str] = None
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    sub_base: Optional[str] = None
    status: bool
    model_config = ConfigDict(from_attributes=True)


class UserUpdatePayload(BaseModel):
    """Campos aceitos para atualização parcial do usuário."""
    nome: Optional[str] = Field(default=None, description="Nome do usuário")
    sobrenome: Optional[str] = Field(default=None, description="Sobrenome do usuário")
    contato: Optional[str] = Field(default=None, description="Telefone ou contato")
    email: Optional[EmailStr] = Field(default=None, description="E-mail do usuário")
    model_config = ConfigDict(from_attributes=True)


class PasswordChangePayload(BaseModel):
    """Schema para alteração de senha."""
    current_password: str = Field(min_length=1, description="Senha atual do usuário")
    new_password: str = Field(
        min_length=8,
        description="Nova senha (mínimo 8 caracteres, deve possuir letras e números)",
    )
    model_config = ConfigDict(from_attributes=True)


# =========================
# POST /users
# =========================
@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    """Cria um novo usuário.

    Valida unicidade de email e username e armazena hash da senha.
    """
    # Log seguro (sem expor senha)
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

    # Campos obrigatórios (pydantic já valida, mas adiciona mensagem)
    if not (body.email and body.password_hash and body.username and body.contato):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email, senha, username e contato são obrigatórios",
        )

    try:
        hashed_password = get_password_hash(body.password_hash)
        obj = User(
            email=body.email,
            password_hash=hashed_password,
            username=body.username,
            contato=body.contato,
            nome=body.nome,
            sobrenome=body.sobrenome,
            status=True if body.status is None else body.status,
            sub_base=body.sub_base,
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


# =========================
# GET /users/{user_id}
# =========================
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


# =========================
# GET /users/me
# =========================
@router.get("/me", response_model=UserFull)
def read_current_user(current_user: User = Depends(get_current_user)) -> UserFull:
    """Retorna os dados completos do usuário logado."""
    return current_user


# =========================
# PATCH /users/me
# =========================
@router.patch("/me", response_model=UserFull)
def update_current_user(
    payload: UserUpdatePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserFull:
    """Atualiza parcialmente os dados do usuário logado.

    Campos não enviados no payload serão ignorados. Se email ou contato estiverem em uso por outro
    usuário, retorna erro de conflito.
    """
    # Atualiza campos se presentes
    if payload.nome is not None:
        current_user.nome = payload.nome.strip() or None
    if payload.sobrenome is not None:
        current_user.sobrenome = payload.sobrenome.strip() or None
    if payload.contato is not None:
        contato = payload.contato.strip()
        if not contato:
            raise HTTPException(status_code=400, detail="O campo 'contato' não pode ficar vazio.")
        other = db.query(User).filter(User.contato == contato, User.id != current_user.id).first()
        if other:
            raise HTTPException(status_code=409, detail="Já existe um usuário com esse contato.")
        current_user.contato = contato
    if payload.email is not None:
        email = payload.email.strip()
        if not email:
            raise HTTPException(status_code=400, detail="O campo 'email' não pode ficar vazio.")
        other = db.query(User).filter(User.email == email, User.id != current_user.id).first()
        if other:
            raise HTTPException(status_code=409, detail="Já existe um usuário com esse e-mail.")
        current_user.email = email

    db.commit()
    db.refresh(current_user)
    return current_user


# =========================
# POST /users/me/password
# =========================
@router.post("/me/password")
def change_password(
    payload: PasswordChangePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Altera a senha do usuário logado.

    Valida a senha atual e substitui por uma nova. Por padrão, não exige confirmação de senha.
    """
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Senha atual incorreta.")

    current_user.password_hash = get_password_hash(payload.new_password)
    db.commit()

    return {"ok": True, "message": "Senha alterada com sucesso"}