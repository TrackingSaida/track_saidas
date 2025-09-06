from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, Request, Response, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import or_

from jose import jwt, JWTError
from passlib.context import CryptContext

from main import get_db                      # sua Session
from users_routes import User                # seu modelo User

# ===== Config (use .env / Render Environment) =====
JWT_ALGORITHM = "HS256"
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret")  # defina em produção
# Ative cookies Secure automaticamente se ENVIRONMENT=prod/production
SECURE_COOKIES = os.getenv("ENVIRONMENT", "dev").lower() in ("prod", "production")

COOKIE_NAME = "access_token"
COOKIE_SAMESITE = "lax"
COOKIE_PATH = "/"
EXP_1H  = 60 * 60
EXP_30D = 30 * 24 * 60 * 60

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
router = APIRouter()

# ===== Schemas =====
class LoginIn(BaseModel):
    username: str = Field(..., description="email ou username")
    password: str
    remember: bool = False

class UserPublic(BaseModel):
    id: int
    username: str | None = None
    email: str | None = None

class LoginOut(BaseModel):
    ok: bool
    user: UserPublic

class MeOut(BaseModel):
    id: int
    username: str | None = None
    email: str | None = None
    roles: List[str] = ["user"]

# ===== Helpers =====
def create_access_token(*, sub: int | str, expires_in_seconds: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": str(sub), "exp": now + timedelta(seconds=expires_in_seconds), "iat": now}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def set_auth_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=SECURE_COOKIES,
        samesite=COOKIE_SAMESITE,
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

def verify_password(plain: str, password_hash: str | None) -> bool:
    return bool(password_hash) and pwd_context.verify(plain, password_hash)

# ===== Dependência de segurança =====
def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Não autenticado")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Não autenticado")
    except JWTError:
        raise HTTPException(status_code=401, detail="Não autenticado")

    user = db.query(User).filter(User.id == int(sub)).first()
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")
    return user

# ===== Endpoints =====
@router.post("/api/auth/login", response_model=LoginOut)
def login(payload: LoginIn, response: Response, db: Session = Depends(get_db)):
    ident = payload.username.strip()
    user = db.query(User).filter(or_(User.username == ident, User.email == ident)).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Credenciais inválidas")

    exp_seconds = EXP_30D if payload.remember else EXP_1H
    token = create_access_token(sub=user.id, expires_in_seconds=exp_seconds)
    set_auth_cookie(response, token, max_age=exp_seconds)

    return {"ok": True, "user": {"id": user.id, "username": user.username, "email": user.email}}

@router.get("/api/auth/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "username": user.username, "email": user.email, "roles": ["user"]}

@router.post("/api/auth/logout")
def logout(response: Response):
    clear_auth_cookie(response)
    return {"ok": True}
