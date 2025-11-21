from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field, AliasChoices, ConfigDict
from passlib.context import CryptContext
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from sqlalchemy import select, or_

from db import get_db
from models import User

# ======================
# Configurações JWT
# ======================
SECRET_KEY = os.getenv("SECRET_KEY", "troque-esta-chave-em-producao")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
REMEMBER_ME_EXPIRE_DAYS = int(os.getenv("REMEMBER_ME_EXPIRE_DAYS", "30"))

# Cookies
ACCESS_COOKIE_NAME = os.getenv("ACCESS_COOKIE_NAME", "access_token")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN")  # ex.: ".seu-dominio.com"

# ======================
# Hash de Senhas
# ======================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

# ======================
# HTTP Bearer (fallback)
# ======================
security = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/auth", tags=["Authentication"])

# ======================
# Schemas
# ======================
class Token(BaseModel):
    access_token: str
    token_type: str

class UserLogin(BaseModel):
    identifier: str = Field(
        min_length=1,
        validation_alias=AliasChoices("email", "username", "contato"),
        serialization_alias="email",
        description="Aceita email, username ou contato",
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

# ======================
# JWT Util
# ======================
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# ======================
# Buscar usuário
# ======================
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

# ======================
# Usuário logado
# ======================
async def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> User:

    token: Optional[str] = request.cookies.get(ACCESS_COOKIE_NAME)

    if not token and credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials

    if not token:
        raise HTTPException(status_code=401, detail="Não autenticado")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub_value: str = payload.get("sub")
        if not sub_value:
            raise HTTPException(status_code=401, detail="Token inválido (sem sub)")
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")

    user = get_user_by_identifier(db, sub_value)
    if not user:
        raise HTTPException(status_code=401, detail="Usuário não encontrado")

    return user

# ======================
# Rotas de autenticação
# ======================
@router.post("/token", response_model=Token)
async def login_for_access_token(user_credentials: UserLogin, db: Session = Depends(get_db)):
    user = authenticate_user(db, user_credentials.identifier, user_credentials.password)
    if not user:
        raise HTTPException(status_code=401, detail="Login ou senha incorretos")

    subject = user.email or user.username or user.contato
    expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    access_token = create_access_token({"sub": subject}, expires)
    return {"access_token": access_token, "token_type": "bearer"}

@router.post("/login")
async def login_set_cookie(user_credentials: UserLogin, response: Response, db: Session = Depends(get_db)):
    user = authenticate_user(db, user_credentials.identifier, user_credentials.password)
    if not user:
        raise HTTPException(status_code=401, detail="Login ou senha incorretos")

    subject = user.email or user.username or user.contato

    expires = (
        timedelta(days=REMEMBER_ME_EXPIRE_DAYS)
        if user_credentials.remember
        else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    token = create_access_token({"sub": subject}, expires)

    samesite = "None" if COOKIE_SECURE else "Lax"

    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=samesite,
        max_age=int(expires.total_seconds()),
        path="/",
        domain=COOKIE_DOMAIN,
    )

    return {
        "ok": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "username": user.username,
            "contato": user.contato,
        }
    }

@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(
        key=ACCESS_COOKIE_NAME,
        path="/",
        domain=COOKIE_DOMAIN,
    )
    return {"ok": True}

@router.get("/me", response_model=UserResponse)
async def read_users_me(current_user: User = Depends(get_current_user)):
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        username=current_user.username,
        contato=current_user.contato,
        role=current_user.role
    )

# ======================
# RESET PASSWORD
# ======================
class ResetPasswordPayload(BaseModel):
    identifier: str = Field(..., description="email, username ou contato")
    new_password: str = Field(min_length=8, description="Nova senha")

@router.post("/reset-password")
async def reset_password(payload: ResetPasswordPayload, db: Session = Depends(get_db)):
    user = get_user_by_identifier(db, payload.identifier)
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    user.password_hash = get_password_hash(payload.new_password)
    db.commit()

    return {"ok": True, "message": "Senha redefinida com sucesso"}
