from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from db import get_db
from models import User

# ======================
# Configurações JWT
# ======================
SECRET_KEY = os.getenv("SECRET_KEY", "sua-chave-secreta-super-segura-aqui-mude-em-producao")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REMEMBER_ME_EXPIRE_DAYS = 30

# Nome do cookie
ACCESS_COOKIE_NAME = "access_token"

# Em produção (HTTPS), deixe como True
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"

# ======================
# Hash de senhas
# ======================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ======================
# Autenticação Bearer
# ======================
security = HTTPBearer(auto_error=False)  # <- NÃO lançar erro automático: vamos tentar cookie antes

router = APIRouter(prefix="/auth", tags=["Authentication"])


# ======================
# Schemas
# ======================
class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    email: Optional[str] = None


class UserLogin(BaseModel):
    email: str
    password: str
    remember: bool = False


class UserResponse(BaseModel):
    id: int
    email: str
    username: Optional[str] = None
    contato: Optional[str] = None


# ======================
# Utilitários de senha
# ======================
def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


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
    return db.query(User).filter(User.email == email).first()


def authenticate_user(db: Session, email: str, password: str) -> Optional[User]:
    user = get_user_by_email(db, email)
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
    token: Optional[str] = None

    # 1) Prioriza COOKIE
    token = request.cookies.get(ACCESS_COOKIE_NAME)

    # 2) Se não tiver cookie, tenta Bearer
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
        email: str = payload.get("sub")
        if email is None:
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

# Login via BEARER (já existia) - útil para clientes programáticos
@router.post("/token", response_model=Token)
async def login_for_access_token(user_credentials: UserLogin, db: Session = Depends(get_db)):
    user = authenticate_user(db, user_credentials.email, user_credentials.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou senha incorretos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": user.email}, expires_delta=expires)
    return {"access_token": access_token, "token_type": "bearer"}


# Login que SETA COOKIE (para navegação do browser)
@router.post("/login")
async def login_set_cookie(user_credentials: UserLogin, response: Response, db: Session = Depends(get_db)):
    user = authenticate_user(db, user_credentials.email, user_credentials.password)
    if not user:
        raise HTTPException(status_code=401, detail="Email ou senha incorretos")

    if user_credentials.remember:
        expires = timedelta(days=REMEMBER_ME_EXPIRE_DAYS)
        max_age = int(expires.total_seconds())
    else:
        expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        max_age = int(expires.total_seconds())

    token = create_access_token(data={"sub": user.email}, expires_delta=expires)

    # Set-Cookie: HTTP-Only

    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=True,       # obrigatório com SameSite=None (e HTTPS)
        samesite="None",   # necessário para cross-site
        max_age=max_age,
        path="/",
)

    #response.set_cookie(
        #key=ACCESS_COOKIE_NAME,
        #value=token,
        #httponly=True,
        #secure=COOKIE_SECURE,   # True em produção (HTTPS)
        #samesite="Lax",
        #max_age=max_age,
        #path="/",
    #)
    # Retorno opcional com dados do usuário
    return {
        "ok": True,
        "user": {"id": user.id, "email": user.email, "username": user.username, "contato": user.contato},
    }


# Logout: apaga o cookie
@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key=ACCESS_COOKIE_NAME, path="/")
    return {"ok": True}


# Quem sou eu (funciona com cookie OU bearer)
@router.get("/me", response_model=UserResponse)
async def read_users_me(current_user: User = Depends(get_current_user)):
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        username=current_user.username,
        contato=current_user.contato,
    )
