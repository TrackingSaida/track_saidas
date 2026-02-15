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
from base import _resolve_user_sub_base

router = APIRouter(prefix="/users", tags=["Users"])
logger = logging.getLogger("routes.users")


# ============================================================
# Schemas
# ============================================================

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=4)
    username: str = Field(min_length=3)
    contato: str

    nome: Optional[str] = None
    sobrenome: Optional[str] = None

    # frontend só permite: admin=1, operador=2, coletador=3
    role: int = Field(default=2)

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
    role: Optional[int] = None
    coletador: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class UserFull(UserOut):
    pass


class AdminUserUpdate(BaseModel):
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    username: Optional[str] = None
    contato: Optional[str] = None
    email: Optional[EmailStr] = None
    status: Optional[bool] = None
    role: Optional[int] = None  # 1,2 ou 3

    model_config = ConfigDict(from_attributes=True)


class UserUpdatePayload(BaseModel):
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    contato: Optional[str] = None
    email: Optional[EmailStr] = None

    model_config = ConfigDict(from_attributes=True)


class PasswordChangePayload(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)
    model_config = ConfigDict(from_attributes=True)


# ============================================================
# POST /users — CRIAR USUÁRIO COM SUB_BASE AUTOMÁTICA
# ============================================================

@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Cria usuário herdando sub_base e setando coletador baseado no role."""

    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(400, "Usuário atual não possui sub_base.")

    # Owner válido
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if not owner:
        raise HTTPException(400, f"Não existe Owner para a sub_base '{sub_base}'.")
    if not owner.ativo:
        raise HTTPException(403, "Owner desta sub_base está inativo.")

    # Emails e usernames únicos
    if db.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(409, "Email já existe.")

    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "Username já existe.")

    # --- MAPEAR ROLE → COLETADOR ---
    coletador = (body.role == 3)

    try:
        new_user = User(
            email=body.email,
            password_hash=get_password_hash(body.password),
            username=body.username,
            contato=body.contato,
            nome=body.nome,
            sobrenome=body.sobrenome,
            status=True,
            role=body.role,
            coletador=coletador,
            sub_base=sub_base
        )

        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        return {"ok": True, "id": new_user.id}

    except Exception as e:
        db.rollback()
        logger.exception("Erro ao criar usuário: %s", e)
        raise HTTPException(500, "Erro interno ao criar usuário.")


# ============================================================
# GET /users/me
# ============================================================

@router.get("/me", response_model=UserFull)
def read_current_user(current_user: User = Depends(get_current_user)):
    return current_user


# ============================================================
# LISTAR USERS — APENAS MESMA SUB_BASE
# ============================================================

@router.get("/all", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Lista usuários apenas da mesma sub_base do solicitante (sub_base obtida do banco)."""
    if current_user.role not in (0, 1):
        raise HTTPException(403, "Apenas admin podem listar usuários.")

    sub_base = _resolve_user_sub_base(db, current_user)
    if not sub_base or not str(sub_base).strip():
        raise HTTPException(403, "Usuário sem sub_base definida. Faça login novamente.")
    return db.scalars(
        select(User).where(User.sub_base == sub_base)
    ).all()


# ============================================================
# GET USER BY ID — respeita sub_base
# ============================================================

@router.get("/{user_id}", response_model=UserOut)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Usuário não encontrado")

    if user.sub_base != current_user.sub_base:
        raise HTTPException(403, "Acesso negado.")

    return user


# ============================================================
# PATCH /users/{id} — Atualização ADMIN
# ============================================================

@router.patch("/{user_id}", response_model=UserOut)
def admin_update_user(
    user_id: int,
    payload: AdminUserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in (0, 1):
        raise HTTPException(403, "Acesso negado.")

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Usuário não encontrado")

    if user.sub_base != current_user.sub_base:
        raise HTTPException(403, "Acesso negado.")

    updates = payload.model_dump(exclude_unset=True)

    # ROLE → define COLETADOR
    if "role" in updates:
        user.role = updates["role"]
        user.coletador = (updates["role"] == 3)

    # outros campos
    for field, value in updates.items():
        if field == "role":
            continue
        setattr(user, field, value)

    db.commit()
    db.refresh(user)
    return user


# ============================================================
# DELETE USER
# ============================================================

@router.delete("/{user_id}", status_code=200)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in (0, 1):
        raise HTTPException(403, "Acesso negado.")

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Usuário não encontrado")

    if user.sub_base != current_user.sub_base:
        raise HTTPException(403, "Acesso negado.")

    db.delete(user)
    db.commit()
    return {"ok": True, "deleted": user_id}


# ============================================================
# PATCH /users/me
# ============================================================

@router.patch("/me", response_model=UserFull)
def update_current_user(
    payload: UserUpdatePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload.nome is not None:
        current_user.nome = payload.nome.strip() or None

    if payload.sobrenome is not None:
        current_user.sobrenome = payload.sobrenome.strip() or None

    if payload.contato is not None:
        contato = payload.contato.strip()
        if not contato:
            raise HTTPException(400, "Contato não pode ser vazio.")

        exists = db.query(User).filter(User.contato == contato, User.id != current_user.id).first()
        if exists:
            raise HTTPException(409, "Contato já em uso.")
        current_user.contato = contato

    if payload.email is not None:
        email = payload.email.strip()
        if not email:
            raise HTTPException(400, "Email não pode ser vazio.")

        exists = db.query(User).filter(User.email == email, User.id != current_user.id).first()
        if exists:
            raise HTTPException(409, "Email já em uso.")
        current_user.email = email

    db.commit()
    db.refresh(current_user)
    return current_user


# ============================================================
# POST /users/me/password
# ============================================================

@router.post("/me/password")
def change_password(
    payload: PasswordChangePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(401, "Senha atual incorreta.")

    current_user.password_hash = get_password_hash(payload.new_password)
    db.commit()
    return {"ok": True, "message": "Senha alterada com sucesso"}
