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
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN")  # ex.: ".seu-dominio.com" (opcional)

# ======================
# Hash de senhas
# ======================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

# ======================
# Autenticação Bearer
# ======================
security = HTTPBearer(auto_error=False)  # tenta cookie antes

router = APIRouter(prefix="/auth", tags=["Authentication"])

# ======================
# Schemas
# ======================
class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    email: Optional[EmailStr] = None

class UserLogin(BaseModel):
    # aceita "email" OU "username" OU "contato" no mesmo campo
    identifier: str = Field(
        min_length=1,
        validation_alias=AliasChoices("email", "username", "contato"),
        serialization_alias="email",  # Swagger exibirá como "email"
        description="Aceita email, username ou contato",
    )
    password: str
    remember: bool = False

    model_config = ConfigDict(from_attributes=True)

class UserResponse(BaseModel):
    id: int
    email: EmailStr
    username: Optional[str] = None
    contato: Optional[str] = None

# ======================
# Utilitários JWT
# ======================
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# ======================
# Acesso a usuário
# ======================
def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.scalars(select(User).where(User.email == email)).first()

def get_user_by_identifier(db: Session, identifier: str) -> Optional[User]:
    """
    Busca por email, username OU contato (primeiro que bater).
    """
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
# Resolver usuário (Cookie OU Bearer)
# ======================
async def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> User:
    token: Optional[str] = request.cookies.get(ACCESS_COOKIE_NAME)

    # Se não tiver cookie, tenta Bearer
    if not token and credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Não autenticado",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")  # subject = email
        if not email:
            raise HTTPException(status_code=401, detail="Token inválido")
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")

    user = get_user_by_email(db, email=email)
    if user is None:
        raise HTTPException(status_code=401, detail="Usuário não encontrado")
    return user

# ======================
# Rotas
# ======================

@router.post("/token", response_model=Token)
async def login_for_access_token(user_credentials: UserLogin, db: Session = Depends(get_db)):
    user = authenticate_user(db, user_credentials.identifier, user_credentials.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Login ou senha incorretos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": user.email}, expires_delta=expires)
    return {"access_token": access_token, "token_type": "bearer"}

@router.post("/login")
async def login_set_cookie(user_credentials: UserLogin, response: Response, db: Session = Depends(get_db)):
    user = authenticate_user(db, user_credentials.identifier, user_credentials.password)
    if not user:
        raise HTTPException(status_code=401, detail="Login ou senha incorretos")

    if user_credentials.remember:
        expires = timedelta(days=REMEMBER_ME_EXPIRE_DAYS)
    else:
        expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    token = create_access_token(data={"sub": user.email}, expires_delta=expires)

    # SameSite/Lax vs None conforme ambiente
    samesite = "None" if COOKIE_SECURE else "Lax"

    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,  # True em produção (HTTPS)
        samesite=samesite,
        max_age=int(expires.total_seconds()),
        path="/",
        domain=COOKIE_DOMAIN,  # None por padrão; configure se precisar subdomínios
    )

    return {
        "ok": True,
        "user": {"id": user.id, "email": user.email, "username": user.username, "contato": user.contato},
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
    )
