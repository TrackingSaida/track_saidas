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
from models import User, Owner

router = APIRouter(prefix="/users", tags=["Users"])

logger = logging.getLogger("routes.users")


# =========================
# Schemas
# =========================

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=4, description="Senha em claro; será hasheada")
    username: str = Field(min_length=3)
    contato: str

    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    status: Optional[bool] = True

    # Agora obrigatório
    sub_base: str = Field(min_length=1, description="sub_base deve já existir e ter Owner")

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


class UserFull(BaseModel):
    id: int
    email: Optional[EmailStr] = None
    username: Optional[str] = None
    contato: Optional[str] = None
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    sub_base: Optional[str] = None
    status: Optional[bool] = None
    model_config = ConfigDict(from_attributes=True)


class UserUpdatePayload(BaseModel):
    nome: Optional[str] = Field(default=None)
    sobrenome: Optional[str] = Field(default=None)
    contato: Optional[str] = Field(default=None)
    email: Optional[EmailStr] = Field(default=None)
    model_config = ConfigDict(from_attributes=True)


class PasswordChangePayload(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)
    model_config = ConfigDict(from_attributes=True)


# =========================
# POST /users — CRIAR USUÁRIO
# =========================

@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    """Cria um novo usuário garantindo a existência de Owner na sub_base."""

    # Log seguro
    try:
        payload_log = body.model_dump(exclude={"password"})
        payload_log["password"] = "***"
    except Exception:
        payload_log = "erro ao processar payload"
    logger.info("POST /users payload=%s", payload_log)

    # Unicidade de email
    exists_email = db.scalars(select(User).where(User.email == body.email)).first()
    if exists_email:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="endereço de email já existente"
        )

    # Unicidade de username
    exists_username = db.scalars(select(User).where(User.username == body.username)).first()
    if exists_username:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="username já existe"
        )

    # ###############################
    # 🔒 VALIDAÇÃO NOVA — OWNER OBRIGATÓRIO
    # ###############################
    owner = db.scalar(select(Owner).where(Owner.sub_base == body.sub_base))
    if not owner:
        raise HTTPException(
            status_code=400,
            detail=f"Não existe Owner cadastrado para a sub_base '{body.sub_base}'."
        )

    if owner.ativo is False:
        raise HTTPException(
            status_code=403,
            detail=f"O Owner da sub_base '{body.sub_base}' está inativo."
        )

    # ===============================
    # Criar usuário
    # ===============================
    try:
        hashed_password = get_password_hash(body.password)
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
# GET /users/me
# =========================
@router.get("/me", response_model=UserFull)
def read_current_user(current_user: User = Depends(get_current_user)) -> UserFull:
    return current_user


# =========================
# GET /users/{user_id}
# =========================
@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuário não encontrado"
        )
    return user


# =========================
# PATCH /users/me
# =========================
@router.patch("/me", response_model=UserFull)
def update_current_user(
    payload: UserUpdatePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserFull:

    if payload.nome is not None:
        current_user.nome = payload.nome.strip() or None

    if payload.sobrenome is not None:
        current_user.sobrenome = payload.sobrenome.strip() or None

    if payload.contato is not None:
        contato = payload.contato.strip()
        if not contato:
            raise HTTPException(400, "O campo 'contato' não pode ficar vazio.")
        other = db.query(User).filter(User.contato == contato, User.id != current_user.id).first()
        if other:
            raise HTTPException(409, "Já existe um usuário com esse contato.")
        current_user.contato = contato

    if payload.email is not None:
        email = payload.email.strip()
        if not email:
            raise HTTPException(400, "O campo 'email' não pode ficar vazio.")
        other = db.query(User).filter(User.email == email, User.id != current_user.id).first()
        if other:
            raise HTTPException(409, "Já existe um usuário com esse e-mail.")
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
):

    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Senha atual incorreta.")

    current_user.password_hash = get_password_hash(payload.new_password)
    db.commit()

    return {"ok": True, "message": "Senha alterada com sucesso"}
