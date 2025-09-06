# auth.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, Request, Response, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import or_

from jose import jwt, JWTError
from passlib.context import CryptContext

# ---------------------------------------------------------------------
# Ajuste estes imports conforme sua estrutura:
# from main import get_db              # função que retorna a Session (SQLAlchemy)
# from models import User              # seu modelo User (SQLAlchemy)
# ---------------------------------------------------------------------

# ===== CONFIG =====
JWT_ALGORITHM = "HS256"
JWT_SECRET = "CHANGE_ME_USE_ENV_VAR"   # -> coloque em variável de ambiente
COOKIE_NAME = "access_token"

# Ative Secure=True em produção/HTTPS
SECURE_COOKIES = False  # mude para True em produção
COOKIE_SAMESITE = "lax"
COOKIE_PATH = "/"

# Lembre: 60 minutos ou 30 dias (em segundos)
EXP_1H   = 60 * 60
EXP_30D  = 30 * 24 * 60 * 60

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

router = APIRouter()

# ====== Schemas ======
class LoginIn(BaseModel):
    username: str = Field(..., description="email ou username")
    password: str
    remember: bool = False

class UserPublic(BaseModel):
    id: int
    username: str
    email: str

class LoginOut(BaseModel):
    ok: bool
    user: UserPublic

class MeOut(BaseModel):
    id: int
    username: str
    email: str
    roles: List[str] = ["user"]

# ====== Helpers ======
def create_access_token(*, sub: str | int, expires_in_seconds: int) -> str:
    now = datetime.now(tz=timezone.utc)
    exp = now + timedelta(seconds=expires_in_seconds)
    payload = {
        "sub": str(sub),
        "exp": exp,
        "iat": now,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token

def verify_password(plain: str, password_hash: str) -> bool:
    return pwd_context.verify(plain, password_hash)

def set_auth_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=SECURE_COOKIES,
        samesite=COOKIE_SAMESITE,  # "lax"
        path=COOKIE_PATH,
    )

def clear_auth_cookie(response: Response) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value="",
        max_age=0,
        expires=0,
        httponly=True,
        secure=SECURE_COOKIES,
        samesite=COOKIE_SAMESITE,
        path=COOKIE_PATH,
    )

# ===== Dependência de segurança =====
def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Não autenticado")

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Não autenticado")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Não autenticado")

    # Busque seu usuário no DB (ajuste conforme seu ORM)
    user = db.query(User).filter(User.id == int(sub)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Não autenticado")

    return user

# ===== Endpoints =====

@router.post("/api/auth/login", response_model=LoginOut)
def login(payload: LoginIn, response: Response, db: Session = Depends(get_db)):
    """
    Autentica por username OU email, confere senha (bcrypt),
    gera JWT e seta cookie HttpOnly.
    """
    identifier = payload.username.strip()

    user = (
        db.query(User)
        .filter(or_(User.username == identifier, User.email == identifier))
        .first()
    )
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")

    if not verify_password(payload.password, user.password_hash):
        # (Opcional) aqui você pode registrar tentativa p/ anti brute-force
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")

    exp_seconds = EXP_30D if payload.remember else EXP_1H
    token = create_access_token(sub=user.id, expires_in_seconds=exp_seconds)
    set_auth_cookie(response, token, max_age=exp_seconds)

    return LoginOut(
        ok=True,
        user=UserPublic(id=user.id, username=user.username, email=user.email)
    )

@router.get("/api/auth/me", response_model=MeOut)
def me(user = Depends(get_current_user)):
    roles = getattr(user, "roles", None)
    roles_out = roles if roles else ["user"]
    return MeOut(id=user.id, username=user.username, email=user.email, roles=roles_out)

@router.post("/api/auth/logout")
def logout(response: Response):
    clear_auth_cookie(response)
    return {"ok": True}

@router.get("/api/health")
def health():
    return {"status": "ok"}
