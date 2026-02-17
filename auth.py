from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from decimal import Decimal

from fastapi import (
    APIRouter, Depends, HTTPException,
    Request, Response
)
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.security import OAuth2PasswordBearer

from pydantic import BaseModel, EmailStr, Field, AliasChoices, ConfigDict

from passlib.context import CryptContext
from jose import JWTError, jwt

from sqlalchemy.orm import Session
from sqlalchemy import select, or_

from db import get_db
from models import User, Owner, Motoboy, MotoboySubBase


# ======================================================
# OAuth2 – Token
# ======================================================
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# ======================================================
# JWT – CONFIGURAÇÃO OFICIAL (ENV ONLY)
# ======================================================
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY não configurada no ambiente")

ALGORITHM = "HS256"

ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
REMEMBER_ME_EXPIRE_DAYS = int(os.getenv("REMEMBER_ME_EXPIRE_DAYS", "30"))

# Cookies
ACCESS_COOKIE_NAME = os.getenv("ACCESS_COOKIE_NAME", "access_token")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() in ("1", "true", "yes")
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN")  # normalmente vazio no Render


# ======================================================
# Password hashing
# ======================================================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# ======================================================
# HTTP Bearer (fallback p/ Authorization header)
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
        description="Aceita email, username ou contato"
    )
    password: str
    remember: bool = False
    model_config = ConfigDict(from_attributes=True)


class MotoboyLogin(BaseModel):
    identifier: str = Field(min_length=1, description="Email, username ou contato")
    password: str


class MotoboySelectSubBase(BaseModel):
    identifier: str = Field(min_length=1)
    password: str
    sub_base: str = Field(min_length=1)


class UserResponse(BaseModel):
    id: int
    email: Optional[EmailStr]
    username: Optional[str]
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    contato: Optional[str]
    role: Optional[int]
    sub_base: Optional[str]
    ignorar_coleta: bool = False
    modo_operacao: Optional[str] = None


# ======================================================
# JWT helpers
# ======================================================
def create_access_token(data: dict, expires_delta: timedelta) -> str:
    to_encode = data.copy()
    to_encode["exp"] = datetime.utcnow() + expires_delta
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def _subject(user: User) -> str:
    return user.email or user.username or user.contato


def _owner_for_sub_base(db: Session, sub_base: str) -> Owner:
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if not owner:
        raise HTTPException(403, "Nenhum Owner encontrado para esta sub_base")
    if owner.ativo is False:
        raise HTTPException(403, "owner_blocked")
    return owner


def _claims(user: User, owner: Owner) -> Dict[str, Any]:
    """
    Tudo que o backend precisa no caminho crítico
    fica resolvido aqui, no login.
    """
    return {
        "sub": _subject(user),
        "uid": user.id,
        "username": user.username,
        "email": user.email,
        "contato": user.contato,
        "role": user.role,
        "sub_base": user.sub_base,
        "ignorar_coleta": bool(owner.ignorar_coleta),
        "owner_ativo": bool(owner.ativo),
        "modo_operacao": (owner.modo_operacao or "codigo") if hasattr(owner, "modo_operacao") else "codigo",
        # valor SEMPRE como string (Decimal-safe)
        "owner_valor": str(owner.valor or 0),
    }


def _claims_motoboy(user: User, motoboy: Motoboy, owner: Owner, sub_base: str) -> Dict[str, Any]:
    """Claims para JWT de motoboy (role=4)."""
    pode_ler_coleta = motoboy.pode_ler_coleta
    if owner.ignorar_coleta:
        pode_ler_coleta = False
    return {
        "sub": _subject(user),
        "uid": user.id,
        "username": user.username,
        "email": user.email,
        "contato": user.contato,
        "role": 4,
        "motoboy_id": motoboy.id_motoboy,
        "sub_base": sub_base,
        "pode_ler_coleta": bool(pode_ler_coleta),
        "pode_ler_saida": bool(motoboy.pode_ler_saida),
        "ignorar_coleta": bool(owner.ignorar_coleta),
        "owner_ativo": bool(owner.ativo),
        "modo_operacao": (owner.modo_operacao or "codigo") if hasattr(owner, "modo_operacao") else "codigo",
        "owner_valor": str(owner.valor or 0),
    }


def _user_from_claims(payload: Dict[str, Any]) -> User:
    """
    User leve (não persistido), montado apenas a partir do JWT.
    Evita qualquer SELECT no auth.
    """
    u = User()
    u.id = payload.get("uid")
    u.username = payload.get("username")
    u.email = payload.get("email")
    u.contato = payload.get("contato")
    u.role = payload.get("role")
    u.sub_base = payload.get("sub_base")

    # flags/policies vindas do token
    u.ignorar_coleta = payload.get("ignorar_coleta", False)
    u.owner_valor = Decimal(payload.get("owner_valor", "0"))
    u.modo_operacao = payload.get("modo_operacao", "codigo")

    return u


# ======================================================
# DB helpers (somente para login)
# ======================================================
def get_user_by_identifier(db: Session, identifier: str) -> Optional[User]:
    identifier = (identifier or "").strip()
    if not identifier:
        return None

    stmt = select(User).where(
        or_(
            User.email == identifier,
            User.username == identifier,
            User.contato == identifier
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
# Usuário logado — FAST PATH (SEM BANCO)
# ======================================================
async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> User:

    token: Optional[str] = request.cookies.get(ACCESS_COOKIE_NAME)

    if not token and credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials

    if not token:
        raise HTTPException(status_code=401, detail="Não autenticado")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")

    if not payload.get("owner_ativo", False):
        raise HTTPException(status_code=403, detail="Operação bloqueada")

    # policy disponível para as rotas
    request.state.ignorar_coleta = payload.get("ignorar_coleta", False)

    return _user_from_claims(payload)


# ======================================================
# ROTAS
# ======================================================
@router.post("/token", response_model=Token)
async def login_for_access_token(
    user_credentials: UserLogin,
    db: Session = Depends(get_db),
):
    user = authenticate_user(db, user_credentials.identifier, user_credentials.password)
    if not user:
        raise HTTPException(401, "Login ou senha incorretos")

    if not user.sub_base:
        raise HTTPException(403, "Usuário sem sub_base definida")

    owner = _owner_for_sub_base(db, user.sub_base)

    expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token(_claims(user, owner), expires)

    return {"access_token": token, "token_type": "bearer"}


@router.post("/login")
async def login_set_cookie(
    user_credentials: UserLogin,
    response: Response,
    db: Session = Depends(get_db),
):
    user = authenticate_user(db, user_credentials.identifier, user_credentials.password)
    if not user:
        raise HTTPException(401, "Login ou senha incorretos")

    if not user.sub_base:
        raise HTTPException(403, "Usuário sem sub_base definida")

    owner = _owner_for_sub_base(db, user.sub_base)

    expires = (
        timedelta(days=REMEMBER_ME_EXPIRE_DAYS)
        if user_credentials.remember
        else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    token = create_access_token(_claims(user, owner), expires)

    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="None" if COOKIE_SECURE else "Lax",
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
            "role": user.role,
            "sub_base": user.sub_base,
            "ignorar_coleta": owner.ignorar_coleta,
            "modo_operacao": (owner.modo_operacao or "codigo") if hasattr(owner, "modo_operacao") else "codigo",
        },
    }


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(
        key=ACCESS_COOKIE_NAME,
        path="/",
        domain=COOKIE_DOMAIN,
    )
    return {"ok": True}


# ======================================================
# LOGIN MOTOBOY (role=4) — mobile
# ======================================================

@router.post("/motoboy-login")
async def motoboy_login(
    body: MotoboyLogin,
    db: Session = Depends(get_db),
):
    """
    Login para motoboy (role=4).
    Se tiver 1 sub_base: retorna token.
    Se tiver múltiplas: retorna multiple_sub_base=true e lista para seleção.
    """
    user = authenticate_user(db, body.identifier, body.password)
    if not user:
        raise HTTPException(401, "Login ou senha incorretos")

    if user.role != 4:
        raise HTTPException(403, "Acesso restrito a motoboys.")

    motoboy = db.scalar(select(Motoboy).where(Motoboy.user_id == user.id))
    if not motoboy:
        raise HTTPException(404, "Perfil de motoboy não encontrado.")

    sub_bases_rows = db.scalars(
        select(MotoboySubBase.sub_base).where(
            MotoboySubBase.motoboy_id == motoboy.id_motoboy,
            MotoboySubBase.ativo.is_(True),
        )
    ).all()
    sub_bases = [s for s in sub_bases_rows if s]

    if len(sub_bases) > 1:
        return {"multiple_sub_base": True, "sub_bases": sub_bases}

    if len(sub_bases) == 0:
        raise HTTPException(403, "Motoboy sem sub_base ativa vinculada.")

    sub_base = sub_bases[0]
    owner = _owner_for_sub_base(db, sub_base)
    expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token(_claims_motoboy(user, motoboy, owner, sub_base), expires)
    return {"access_token": token, "token_type": "bearer"}


@router.post("/motoboy-select-subbase", response_model=Token)
async def motoboy_select_subbase(
    body: MotoboySelectSubBase,
    db: Session = Depends(get_db),
):
    """
    Após motoboy-login com multiple_sub_base, o app envia
    identifier + password + sub_base escolhida para obter o token.
    """
    user = authenticate_user(db, body.identifier, body.password)
    if not user:
        raise HTTPException(401, "Login ou senha incorretos")

    if user.role != 4:
        raise HTTPException(403, "Acesso restrito a motoboys.")

    motoboy = db.scalar(select(Motoboy).where(Motoboy.user_id == user.id))
    if not motoboy:
        raise HTTPException(404, "Perfil de motoboy não encontrado.")

    existe = db.scalar(
        select(MotoboySubBase).where(
            MotoboySubBase.motoboy_id == motoboy.id_motoboy,
            MotoboySubBase.sub_base == body.sub_base.strip(),
            MotoboySubBase.ativo.is_(True),
        )
    )
    if not existe:
        raise HTTPException(400, "Sub_base inválida ou inativa para este motoboy.")

    owner = _owner_for_sub_base(db, body.sub_base.strip())
    expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token(
        _claims_motoboy(user, motoboy, owner, body.sub_base.strip()),
        expires,
    )
    return {"access_token": token, "token_type": "bearer"}


def _nome_exibicao(user: User) -> tuple[Optional[str], Optional[str]]:
    """Retorna (nome, sobrenome) para exibição. Quando ambos vazios, deriva do username."""
    nome_val = (getattr(user, "nome", None) or "").strip()
    sobrenome_val = (getattr(user, "sobrenome", None) or "").strip()
    if not nome_val and not sobrenome_val and (user.username or "").strip():
        # Fallback: formata username como nome (ex: joao.silva -> Joao Silva)
        partes = (user.username or "").replace(".", " ").replace("_", " ").split()
        nome_val = " ".join(p.capitalize() for p in partes) if partes else ""
    return (nome_val or None, sobrenome_val or None)


@router.get("/me", response_model=UserResponse)
async def read_users_me(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    nome_val, sobrenome_val = _nome_exibicao(current_user)
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        username=current_user.username,
        nome=nome_val,
        sobrenome=sobrenome_val,
        contato=current_user.contato,
        role=current_user.role,
        sub_base=current_user.sub_base,
        ignorar_coleta=bool(getattr(request.state, "ignorar_coleta", False)),
        modo_operacao=getattr(current_user, "modo_operacao", None) or "codigo",
    )


# ======================================================
# RESET PASSWORD
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
