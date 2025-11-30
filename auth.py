from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import (
    APIRouter, Depends, HTTPException, Request, Response
)
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, OAuth2PasswordBearer

from pydantic import BaseModel, EmailStr, Field, AliasChoices, ConfigDict
from passlib.context import CryptContext
from jose import JWTError, jwt

from sqlalchemy.orm import Session
from sqlalchemy import select, or_

from db import get_db
from models import User, Owner


# ======================================================
# OAuth2 (entregador future use)
# ======================================================
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# ======================================================
# JWT Settings
# ======================================================
SECRET_KEY = os.getenv("SECRET_KEY", "troque-esta-chave-em-producao")
ALGORITHM = "HS256"

ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
REMEMBER_ME_EXPIRE_DAYS = int(os.getenv("REMEMBER_ME_EXPIRE_DAYS", "30"))

# Cookies
ACCESS_COOKIE_NAME = os.getenv("ACCESS_COOKIE_NAME", "access_token")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")

# ======================================================
# Password hashing
# ======================================================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

# ======================================================
# HTTP Bearer (fallback)
# ======================================================
security = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/auth", tags=["Authentication"])


# ======================================================
# Schemas
# ======================================================
class Token(BaseModel):
    access_token: str
    token_type: str

class UserLogin(BaseModel):
    identifier: str = Field(
        min_length=1,
        validation_alias=AliasChoices("email", "username", "contato"),
        serialization_alias="email",
        description="Aceita email, username ou telefone"
    )
    password: str
    remember: bool = False
    model_config = ConfigDict(from_attributes=True)

class UserResponse(BaseModel):
    id: int
    email: Optional[EmailStr]
    username: Optional[str]
    contato: Optional[str]
    role: Optional[int]
    sub_base: Optional[str]
    ignorar_coleta: Optional[bool] = False


# ======================================================
# JWT Utility
# ======================================================
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ======================================================
# Get user by email/username/contato
# ======================================================
def get_user_by_identifier(db: Session, identifier: str) -> Optional[User]:
    identifier = (identifier or "").strip()
    if not identifier:
        return None

    stmt = select(User).where(
        or_(
            User.email == identifier,
            User.username == identifier,
            User.contato == identifier,
        )
    )
    return db.scalars(stmt).first()


def authenticate_user(db: Session, identifier: str, password: str) -> Optional[User]:
    user = get_user_by_identifier(db, identifier)
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


# ======================================================
# get_current_user — usado em TODAS rotas protegidas
# ======================================================
async def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> User:

    # Tentar via cookie
    token: Optional[str] = request.cookies.get(ACCESS_COOKIE_NAME)

    # Fallback Bearer
    if not token and credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials

    if not token:
        raise HTTPException(401, "Não autenticado")

    # Validar token
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub_value = payload.get("sub")
        if not sub_value:
            raise HTTPException(401, "Token inválido (sem sub)")
    except JWTError:
        raise HTTPException(401, "Token inválido ou expirado")

    # Buscar usuário
    user = get_user_by_identifier(db, sub_value)
    if not user:
        raise HTTPException(401, "Usuário não encontrado")

    if not user.sub_base:
        raise HTTPException(403, "Usuário sem sub_base configurada.")

    # Buscar owner
    owner = db.scalar(select(Owner).where(Owner.sub_base == user.sub_base))
    if not owner:
        raise HTTPException(403, "Nenhum Owner encontrado para esta sub_base.")

    if owner.ativo is False:
        raise HTTPException(403, "Operação bloqueada pelo administrador.")

    # Flag ignorar_coleta
    request.state.ignorar_coleta = owner.ignorar_coleta

    return user


# ======================================================
# LOGIN — grava cookie
# ======================================================
@router.post("/login")
async def login_set_cookie(
    user_credentials: UserLogin,
    response: Response,
    db: Session = Depends(get_db)
):
    user = authenticate_user(db, user_credentials.identifier, user_credentials.password)
    if not user:
        raise HTTPException(401, "Login ou senha incorretos")

    if not user.sub_base:
        raise HTTPException(403, "Usuário sem sub_base configurada.")

    owner = db.scalar(select(Owner).where(Owner.sub_base == user.sub_base))
    if not owner:
        raise HTTPException(403, "Nenhum Owner encontrado para esta sub_base.")
    if owner.ativo is False:
        raise HTTPException(403, "owner_blocked")

    subject = user.email or user.username or user.contato

    expires = (
        timedelta(days=REMEMBER_ME_EXPIRE_DAYS)
        if user_credentials.remember else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    token = create_access_token({"sub": subject}, expires)

    samesite = "None" if COOKIE_SECURE else "Lax"

    # 🔥 CORREÇÃO IMPORTANTE — REMOVIDO domain=
    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=samesite,
        max_age=int(expires.total_seconds()),
        path="/",
    )

    return {
        "ok": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "username": user.username,
            "contato": user.contato,
            "role": user.role,
            "sub_base": user.sub_base,
        }
    }


# ======================================================
# TOKEN login (para API externa)
# ======================================================
@router.post("/token", response_model=Token)
async def login_for_access_token(user_credentials: UserLogin, db: Session = Depends(get_db)):
    user = authenticate_user(db, user_credentials.identifier, user_credentials.password)
    if not user:
        raise HTTPException(401, "Login ou senha incorretos")

    subject = user.email or user.username or user.contato
    expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    return {
        "access_token": create_access_token({"sub": subject}, expires),
        "token_type": "bearer"
    }


# ======================================================
# LOGOUT
# ======================================================
@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(
        key=ACCESS_COOKIE_NAME,
        path="/",
    )
    return {"ok": True}


# ======================================================
# /auth/me
# ======================================================
@router.get("/me", response_model=UserResponse)
async def read_users_me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    owner = db.scalar(select(Owner).where(Owner.sub_base == current_user.sub_base))

    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        username=current_user.username,
        contato=current_user.contato,
        role=current_user.role,
        sub_base=current_user.sub_base,
        ignorar_coleta=(owner.ignorar_coleta if owner else False)
    )


# ======================================================
# Reset password
# ======================================================
class ResetPasswordPayload(BaseModel):
    identifier: str
    new_password: str = Field(min_length=8)

@router.post("/reset-password")
async def reset_password(payload: ResetPasswordPayload, db: Session = Depends(get_db)):
    user = get_user_by_identifier(db, payload.identifier)
    if not user:
        raise HTTPException(404, "Usuário não encontrado")

    user.password_hash = get_password_hash(payload.new_password)
    db.commit()

    return {"ok": True, "message": "Senha redefinida com sucesso"}
