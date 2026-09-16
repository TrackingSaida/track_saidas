from __future__ import annotations

import os
import uuid
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from decimal import Decimal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
)
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.security import OAuth2PasswordBearer

from pydantic import BaseModel, EmailStr, Field, AliasChoices, ConfigDict

from passlib.context import CryptContext
from jose import JWTError, jwt

from sqlalchemy.orm import Session
from sqlalchemy import select, or_

from db import get_db
from db_utils import run_db_query_with_retry
from models import User, Owner, Motoboy, MotoboySubBase, MotoboyRefreshToken


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

ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "120"))
REMEMBER_ME_EXPIRE_DAYS = int(os.getenv("REMEMBER_ME_EXPIRE_DAYS", "200"))
# Access curto + refresh silencioso (sessão deslizante). Preferir HOURS; DAYS legado.
MOTOBOY_ACCESS_TOKEN_EXPIRE_HOURS = int(os.getenv("MOTOBOY_ACCESS_TOKEN_EXPIRE_HOURS", "2"))
MOTOBOY_ACCESS_TOKEN_EXPIRE_DAYS = int(os.getenv("MOTOBOY_ACCESS_TOKEN_EXPIRE_DAYS", "0"))
MOTOBOY_REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("MOTOBOY_REFRESH_TOKEN_EXPIRE_DAYS", "60"))
MOTOBOY_IDLE_TIMEOUT_DAYS = int(os.getenv("MOTOBOY_IDLE_TIMEOUT_DAYS", "7"))
MOTOBOY_REFRESH_ABSOLUTE_DAYS = int(os.getenv("MOTOBOY_REFRESH_ABSOLUTE_DAYS", "60"))

CLAIMS_STALE_HEADER = "X-Claims-Stale"
SESSION_INVALID_DETAIL = "Sessão inválida. Faça login novamente."
SESSION_IDLE_DETAIL = "Sessão expirada por inatividade. Faça login novamente."

# Cookies
ACCESS_COOKIE_NAME = os.getenv("ACCESS_COOKIE_NAME", "access_token")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() in ("1", "true", "yes")
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN")  # normalmente vazio no Render


# ======================================================
# Senha padrão
# ======================================================
DEFAULT_PASSWORD = os.getenv("DEFAULT_PASSWORD", "123456")


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
logger = logging.getLogger("auth")


def _identifier_mask(identifier: str) -> str:
    raw = (identifier or "").strip()
    if not raw:
        return ""
    if len(raw) <= 4:
        return "*" * len(raw)
    return f"{raw[:2]}***{raw[-2:]}"


def _identifier_hash(identifier: str) -> str:
    raw = (identifier or "").strip().lower().encode("utf-8")
    if not raw:
        return ""
    return hashlib.sha256(raw).hexdigest()[:12]


def _auth_attempt_meta(identifier: str) -> Dict[str, str]:
    return {
        "attempt_id": uuid.uuid4().hex[:12],
        "identifier_mask": _identifier_mask(identifier),
        "identifier_hash": _identifier_hash(identifier),
    }


# ======================================================
# Schemas
# ======================================================
class Token(BaseModel):
    access_token: str
    token_type: str
    must_change_password: Optional[bool] = None
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None


class MotoboyRefreshBody(BaseModel):
    refresh_token: str = Field(min_length=16)


class MotoboyLogoutBody(BaseModel):
    refresh_token: Optional[str] = None


class UserLogin(BaseModel):
    identifier: str = Field(
        min_length=1,
        validation_alias=AliasChoices("identifier", "email", "username", "contato"),
        serialization_alias="email",
        description="Aceita identifier, email, username ou contato",
    )
    password: str
    remember: bool = False
    model_config = ConfigDict(from_attributes=True)


class MotoboyLogin(BaseModel):
    # Mesmos aliases do UserLogin: app mobile envia identifier; RN/axios às vezes username/email.
    identifier: str = Field(
        min_length=1,
        validation_alias=AliasChoices("identifier", "email", "username", "contato"),
        description="identifier, email, username ou contato",
    )
    password: str


class MotoboySelectSubBase(BaseModel):
    identifier: str = Field(
        min_length=1,
        validation_alias=AliasChoices("identifier", "email", "username", "contato"),
    )
    password: str
    sub_base: str = Field(min_length=1)


class RootSelectSubBase(BaseModel):
    identifier: str = Field(
        min_length=1,
        validation_alias=AliasChoices("identifier", "email", "username", "contato"),
    )
    password: str
    sub_base: str = Field(min_length=1)
    remember: bool = False


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
    tipo_owner: Optional[str] = None
    must_change_password: Optional[bool] = None
    entrada_obrigatoria_habilitada: bool = False
    conferencia_saida_habilitada: bool = False
    bloquear_saida_sem_coleta: bool = False


# ======================================================
# JWT helpers
# ======================================================
def create_access_token(data: dict, expires_delta: timedelta) -> str:
    to_encode = data.copy()
    to_encode["exp"] = datetime.utcnow() + expires_delta
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def _motoboy_access_expires() -> timedelta:
    if MOTOBOY_ACCESS_TOKEN_EXPIRE_HOURS > 0:
        return timedelta(hours=MOTOBOY_ACCESS_TOKEN_EXPIRE_HOURS)
    days = MOTOBOY_ACCESS_TOKEN_EXPIRE_DAYS if MOTOBOY_ACCESS_TOKEN_EXPIRE_DAYS > 0 else 1
    return timedelta(days=days)


def _motoboy_refresh_expires() -> timedelta:
    return timedelta(days=max(1, MOTOBOY_REFRESH_TOKEN_EXPIRE_DAYS))


def _coerce_role_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def bump_motoboy_claims_version(db: Session, motoboy: Motoboy, *, commit: bool = False) -> int:
    current = int(getattr(motoboy, "claims_version", 0) or 0)
    motoboy.claims_version = current + 1
    db.add(motoboy)
    if commit:
        run_db_query_with_retry(db, db.commit)
    return int(motoboy.claims_version)


def bump_motoboys_claims_version_for_sub_base(db: Session, sub_base: str) -> int:
    rows = list(
        db.scalars(select(Motoboy).where(Motoboy.sub_base == (sub_base or "").strip())).all()
    )
    for m in rows:
        m.claims_version = int(getattr(m, "claims_version", 0) or 0) + 1
        db.add(m)
    return len(rows)


def _hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_refresh_token_plain() -> str:
    return secrets.token_urlsafe(48)


def revoke_motoboy_refresh_tokens_for_user(db: Session, user_id: int, *, commit: bool = True) -> None:
    now = datetime.utcnow()
    rows = run_db_query_with_retry(
        db,
        lambda: db.scalars(
            select(MotoboyRefreshToken).where(
                MotoboyRefreshToken.user_id == user_id,
                MotoboyRefreshToken.revoked_at.is_(None),
            )
        ).all(),
    )
    for row in rows:
        row.revoked_at = now
    if rows and commit:
        run_db_query_with_retry(db, db.commit)


def _store_motoboy_refresh_token(
    db: Session,
    *,
    user_id: int,
    motoboy_id: int,
    plain_token: str,
) -> None:
    now = datetime.utcnow()
    expires_at = now + _motoboy_refresh_expires()
    db.add(
        MotoboyRefreshToken(
            user_id=user_id,
            motoboy_id=motoboy_id,
            token_hash=_hash_refresh_token(plain_token),
            expires_at=expires_at,
            last_activity_at=now,
        )
    )


def _issue_motoboy_auth_response(
    db: Session,
    user: User,
    motoboy: Motoboy,
    owner: Owner,
    sub_base: str,
) -> Dict[str, Any]:
    # Espelha a base da sessão no cadastro — refresh e consultas de banco ficam alinhados
    selected = (sub_base or "").strip()
    if selected and (user.sub_base or "").strip() != selected:
        user.sub_base = selected
        db.add(user)

    access_delta = _motoboy_access_expires()
    access_token = create_access_token(
        _claims_motoboy(user, motoboy, owner, selected),
        access_delta,
    )
    revoke_motoboy_refresh_tokens_for_user(db, int(user.id), commit=False)
    refresh_plain = _generate_refresh_token_plain()
    _store_motoboy_refresh_token(
        db,
        user_id=int(user.id),
        motoboy_id=int(motoboy.id_motoboy),
        plain_token=refresh_plain,
    )
    run_db_query_with_retry(db, db.commit)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_plain,
        "expires_in": int(access_delta.total_seconds()),
        "must_change_password": _must_change_password_from_user(user),
    }


def _resolve_motoboy_session_sub_base(
    db: Session,
    *,
    user: User,
    motoboy: Motoboy,
) -> str:
    """
    Resolve sub_base de sessão do motoboy apenas entre vínculos ativos.
    Nunca usa users.sub_base se ela não estiver em MotoboySubBase.
    """
    sub_bases_rows = run_db_query_with_retry(
        db,
        lambda: db.scalars(
            select(MotoboySubBase.sub_base).where(
                MotoboySubBase.motoboy_id == motoboy.id_motoboy,
                MotoboySubBase.ativo.is_(True),
            )
        ).all(),
    )
    sub_bases = sorted({(s or "").strip() for s in sub_bases_rows if (s or "").strip()})
    preferred = (user.sub_base or "").strip()
    if not sub_bases:
        # Legado / dado incompleto: permite claim da sessão se existir
        if preferred:
            return preferred
        raise HTTPException(status_code=403, detail="Motoboy sem sub_base ativa vinculada.")

    if preferred and preferred in sub_bases:
        return preferred
    if len(sub_bases) == 1:
        return sub_bases[0]
    raise HTTPException(
        status_code=403,
        detail="Selecione a base novamente. Sessão com múltiplas bases ativas.",
    )


def ensure_motoboy_session(db: Session, user: User) -> User:
    """
    Garante identidade de motoboy na request:
    - role 4
    - motoboy_id (hidrata do banco se o JWT veio sem o claim — ex.: fallback /auth/token)
    - sub_base alinhada a MotoboySubBase quando possível
    """
    try:
        role = int(user.role) if getattr(user, "role", None) is not None and user.role != "" else None
    except (TypeError, ValueError):
        role = None
    if role != 4:
        raise HTTPException(status_code=403, detail="Acesso restrito a motoboys.")

    motoboy: Optional[Motoboy] = None
    raw_mid = getattr(user, "motoboy_id", None)
    if raw_mid is not None and raw_mid != "":
        try:
            mid = int(raw_mid)
            motoboy = run_db_query_with_retry(db, lambda: db.get(Motoboy, mid))
        except (TypeError, ValueError):
            motoboy = None

    if motoboy is None and getattr(user, "id", None) is not None:
        try:
            uid = int(user.id)
        except (TypeError, ValueError):
            uid = None
        if uid is not None:
            motoboy = run_db_query_with_retry(
                db,
                lambda: db.scalar(select(Motoboy).where(Motoboy.user_id == uid)),
            )

    if motoboy is None:
        raise HTTPException(status_code=403, detail="Token inválido para motoboy.")

    user.motoboy_id = int(motoboy.id_motoboy)
    token_sub_base = (getattr(user, "sub_base", None) or "").strip()
    try:
        user.sub_base = _resolve_motoboy_session_sub_base(db, user=user, motoboy=motoboy)
    except HTTPException as exc:
        # Evita lockout total (ex.: multi-base com preferred stale): mantém claim se houver
        if token_sub_base and exc.status_code == 403:
            logger.warning(
                "motoboy_sub_base_resolve_fallback user_id=%s motoboy_id=%s token_sub_base=%s detail=%s",
                getattr(user, "id", None),
                user.motoboy_id,
                token_sub_base,
                exc.detail,
            )
            user.sub_base = token_sub_base
        else:
            raise
    return user


def _rotate_motoboy_refresh_token(db: Session, plain_refresh: str) -> Dict[str, Any]:
    token_hash = _hash_refresh_token(plain_refresh.strip())
    row = run_db_query_with_retry(
        db,
        lambda: db.scalar(
            select(MotoboyRefreshToken).where(
                MotoboyRefreshToken.token_hash == token_hash,
                MotoboyRefreshToken.revoked_at.is_(None),
            )
        ),
    )
    now = datetime.utcnow()
    if not row or row.expires_at < now:
        raise HTTPException(status_code=401, detail="Refresh token inválido ou expirado")

    # Teto absoluto desde a criação do refresh
    absolute_limit = timedelta(days=max(1, MOTOBOY_REFRESH_ABSOLUTE_DAYS))
    created = getattr(row, "created_at", None) or now
    if created + absolute_limit < now:
        row.revoked_at = now
        run_db_query_with_retry(db, db.commit)
        raise HTTPException(status_code=401, detail=SESSION_INVALID_DETAIL)

    # Idle: sem uso por N dias
    idle_limit = timedelta(days=max(1, MOTOBOY_IDLE_TIMEOUT_DAYS))
    last_act = getattr(row, "last_activity_at", None) or created
    if last_act + idle_limit < now:
        row.revoked_at = now
        run_db_query_with_retry(db, db.commit)
        raise HTTPException(status_code=401, detail=SESSION_IDLE_DETAIL)

    user = run_db_query_with_retry(db, lambda: db.get(User, row.user_id))
    motoboy = run_db_query_with_retry(db, lambda: db.get(Motoboy, row.motoboy_id))
    if not user or not motoboy or user.role != 4:
        raise HTTPException(status_code=401, detail="Refresh token inválido ou expirado")
    if not bool(getattr(user, "status", True)):
        raise HTTPException(status_code=401, detail=SESSION_INVALID_DETAIL)

    sub_base = _resolve_motoboy_session_sub_base(db, user=user, motoboy=motoboy)
    owner = _owner_for_sub_base(db, sub_base)

    row.revoked_at = now
    # Sliding: atividade registrada na rotação do refresh
    response = _issue_motoboy_auth_response(db, user, motoboy, owner, sub_base)
    return response


def _subject(user: User) -> str:
    return user.email or user.username or user.contato


def _owner_for_sub_base(db: Session, sub_base: str) -> Owner:
    owner = run_db_query_with_retry(
        db,
        lambda: db.scalar(select(Owner).where(Owner.sub_base == sub_base)),
    )
    if not owner:
        raise HTTPException(403, "Nenhum Owner encontrado para esta sub_base")
    if owner.ativo is False:
        raise HTTPException(403, "owner_blocked")
    return owner


def _must_change_password_from_user(user: User) -> bool:
    """
    Define se o usuário deve trocar a senha no próximo login.
    Usa apenas o flag persistido no banco (must_change_password).
    Quando False, não obriga troca mesmo que a senha seja a padrão.
    """
    return bool(getattr(user, "must_change_password", False))


def _tipo_owner_from_owner(owner: Owner) -> str:
    v = (getattr(owner, "tipo_owner", None) or "subbase")
    if isinstance(v, str):
        v = v.strip().lower()
    return "base" if v == "base" else "subbase"


def _claims(user: User, owner: Owner, sub_base: Optional[str] = None) -> Dict[str, Any]:
    """
    Tudo que o backend precisa no caminho crítico
    fica resolvido aqui, no login.
    sub_base opcional: usado no login root com seleção de tenant.
    """
    resolved_sub_base = (sub_base or getattr(owner, "sub_base", None) or user.sub_base or "").strip() or None
    return {
        "sub": _subject(user),
        "uid": user.id,
        "username": user.username,
        "email": user.email,
        "contato": user.contato,
        "role": user.role,
        "sub_base": resolved_sub_base,
        "ignorar_coleta": bool(owner.ignorar_coleta),
        "owner_ativo": bool(owner.ativo),
        "modo_operacao": (owner.modo_operacao or "codigo") if hasattr(owner, "modo_operacao") else "codigo",
        "tipo_owner": _tipo_owner_from_owner(owner),
        # valor SEMPRE como string (Decimal-safe)
        "owner_valor": str(owner.valor or 0),
        "must_change_password": _must_change_password_from_user(user),
        "entrada_obrigatoria_habilitada": bool(
            getattr(owner, "entrada_obrigatoria_habilitada", False)
        ),
        "conferencia_saida_habilitada": bool(
            getattr(owner, "conferencia_saida_habilitada", False)
        ),
        "bloquear_saida_sem_coleta": bool(
            getattr(owner, "bloquear_saida_sem_coleta", False)
        ),
    }


def _list_active_owner_sub_bases(db: Session) -> List[str]:
    """Lista sub_bases de Owners ativos (login root)."""
    rows = run_db_query_with_retry(
        db,
        lambda: db.scalars(
            select(Owner.sub_base).where(
                Owner.ativo.is_(True),
                Owner.sub_base.is_not(None),
            )
        ).all(),
    )
    sub_bases = sorted({(s or "").strip() for s in rows if (s or "").strip()})
    return sub_bases


def _root_needs_sub_base_response(db: Session, user: User) -> Dict[str, Any]:
    sub_bases = _list_active_owner_sub_bases(db)
    if not sub_bases:
        raise HTTPException(403, "Nenhuma sub_base ativa disponível.")
    return {
        "needs_sub_base_selection": True,
        "sub_bases": sub_bases,
        "must_change_password": _must_change_password_from_user(user),
    }


def _staff_expires(remember: bool) -> timedelta:
    return (
        timedelta(days=REMEMBER_ME_EXPIRE_DAYS)
        if remember
        else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )


def _set_access_cookie(response: Response, token: str, expires: timedelta) -> None:
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


def _issue_staff_auth(
    user: User,
    owner: Owner,
    expires: timedelta,
    *,
    sub_base: Optional[str] = None,
    response: Optional[Response] = None,
) -> Dict[str, Any]:
    resolved_sub_base = (sub_base or getattr(owner, "sub_base", None) or user.sub_base or "").strip()
    token = create_access_token(_claims(user, owner, resolved_sub_base), expires)
    must_change = _must_change_password_from_user(user)
    if response is not None:
        _set_access_cookie(response, token, expires)
    return {
        "access_token": token,
        "token_type": "bearer",
        "must_change_password": must_change,
        "expires_in": int(expires.total_seconds()),
        "ok": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "username": user.username,
            "contato": user.contato,
            "role": user.role,
            "sub_base": resolved_sub_base,
            "ignorar_coleta": owner.ignorar_coleta,
            "modo_operacao": (owner.modo_operacao or "codigo") if hasattr(owner, "modo_operacao") else "codigo",
            "tipo_owner": _tipo_owner_from_owner(owner),
            "must_change_password": must_change,
        },
    }


def _claims_motoboy(user: User, motoboy: Motoboy, owner: Owner, sub_base: str) -> Dict[str, Any]:
    """Claims para JWT de motoboy (role=4)."""
    pode_realizar_coleta = bool(
        getattr(motoboy, "pode_realizar_coleta", motoboy.pode_ler_coleta)
    )
    modo = (getattr(owner, "modo_operacao", None) or "codigo").strip().lower()
    pode_ler_coleta = pode_realizar_coleta and modo in ("codigo", "ambos")
    if owner.ignorar_coleta:
        pode_ler_coleta = False
        pode_realizar_coleta = False
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
        "pode_realizar_coleta": bool(pode_realizar_coleta),
        "pode_ler_saida": bool(motoboy.pode_ler_saida),
        "pode_digitar_codigo_manual": bool(getattr(motoboy, "pode_digitar_codigo_manual", True)),
        "pode_lancar_avulso": bool(getattr(motoboy, "pode_lancar_avulso", True)),
        "avulso_exige_foto": bool(getattr(motoboy, "avulso_exige_foto", False)),
        "ignorar_coleta": bool(owner.ignorar_coleta),
        "owner_ativo": bool(owner.ativo),
        "modo_operacao": (owner.modo_operacao or "codigo") if hasattr(owner, "modo_operacao") else "codigo",
        "tipo_owner": _tipo_owner_from_owner(owner),
        "owner_valor": str(owner.valor or 0),
        "must_change_password": _must_change_password_from_user(user),
        "devolucao_sub_base_habilitada": bool(
            getattr(owner, "devolucao_sub_base_habilitada", False)
        ),
        "entrada_obrigatoria_habilitada": bool(
            getattr(owner, "entrada_obrigatoria_habilitada", False)
        ),
        "conferencia_saida_habilitada": bool(
            getattr(owner, "conferencia_saida_habilitada", False)
        ),
        "bloquear_saida_sem_coleta": bool(
            getattr(owner, "bloquear_saida_sem_coleta", False)
        ),
        "sub_base_nome": (owner.sub_base or sub_base or "").strip() or sub_base,
        "claims_version": int(getattr(motoboy, "claims_version", 0) or 0),
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
    try:
        role_int = int(payload.get("role")) if payload.get("role") is not None and payload.get("role") != "" else None
    except (TypeError, ValueError):
        role_int = None
    u.role = role_int if role_int is not None else payload.get("role")
    u.sub_base = payload.get("sub_base")
    # role pode vir como "4" no JWT — não perder motoboy_id no register de push
    u.motoboy_id = payload.get("motoboy_id") if role_int == 4 else None

    # flags/policies vindas do token
    u.ignorar_coleta = payload.get("ignorar_coleta", False)
    u.owner_valor = Decimal(payload.get("owner_valor", "0"))
    u.modo_operacao = payload.get("modo_operacao", "codigo")
    u.tipo_owner = (payload.get("tipo_owner") or "subbase").strip().lower()
    if u.tipo_owner not in ("base", "subbase"):
        u.tipo_owner = "subbase"
    u.pode_ler_coleta = bool(payload.get("pode_ler_coleta", False))
    u.pode_realizar_coleta = bool(payload.get("pode_realizar_coleta", u.pode_ler_coleta))
    u.devolucao_sub_base_habilitada = bool(payload.get("devolucao_sub_base_habilitada", False))
    u.entrada_obrigatoria_habilitada = bool(payload.get("entrada_obrigatoria_habilitada", False))
    u.conferencia_saida_habilitada = bool(payload.get("conferencia_saida_habilitada", False))
    u.bloquear_saida_sem_coleta = bool(payload.get("bloquear_saida_sem_coleta", False))
    u.sub_base_nome = (payload.get("sub_base_nome") or payload.get("sub_base") or "").strip() or None
    u.pode_ler_saida = bool(payload.get("pode_ler_saida", True))
    u.pode_digitar_codigo_manual = bool(payload.get("pode_digitar_codigo_manual", True))
    u.pode_lancar_avulso = bool(payload.get("pode_lancar_avulso", True))
    u.avulso_exige_foto = bool(payload.get("avulso_exige_foto", False))

    # flags de senha vindas do token (podem ser sobrescritas por leitura direta em /auth/me)
    u.must_change_password = payload.get("must_change_password", None)

    return u


# ======================================================
# DB helpers (somente para login)
# ======================================================
def get_users_by_identifier(db: Session, identifier: str) -> List[User]:
    identifier = (identifier or "").strip()
    if not identifier:
        return []
    stmt = select(User).where(
        or_(
            User.email == identifier,
            User.username == identifier,
            User.contato == identifier,
        )
    )
    rows = run_db_query_with_retry(db, lambda: db.scalars(stmt).all())
    return list(rows or [])


def get_user_by_identifier(db: Session, identifier: str) -> Optional[User]:
    users = get_users_by_identifier(db, identifier)
    if len(users) == 1:
        return users[0]
    return None


def authenticate_user(db: Session, identifier: str, password: str) -> Optional[User]:
    matched: List[User] = []
    for user in get_users_by_identifier(db, identifier):
        if not bool(getattr(user, "status", True)):
            continue
        hashed = getattr(user, "password_hash", None)
        if not hashed:
            continue
        if verify_password(password, hashed):
            matched.append(user)
    if len(matched) == 1:
        return matched[0]
    return None


def _ensure_staff_jwt_matches_db(db: Session, *, uid: Any, jwt_role: int) -> None:
    try:
        user_id = int(uid) if uid is not None and uid != "" else None
    except (TypeError, ValueError):
        user_id = None
    if user_id is None:
        raise HTTPException(status_code=401, detail=SESSION_INVALID_DETAIL)
    db_user = run_db_query_with_retry(db, lambda: db.get(User, user_id))
    if not db_user:
        raise HTTPException(status_code=401, detail=SESSION_INVALID_DETAIL)
    if not bool(getattr(db_user, "status", True)):
        raise HTTPException(status_code=401, detail=SESSION_INVALID_DETAIL)
    live_role = _coerce_role_int(getattr(db_user, "role", None))
    if live_role == 4:
        raise HTTPException(status_code=401, detail=SESSION_INVALID_DETAIL)
    if live_role != jwt_role:
        raise HTTPException(status_code=401, detail=SESSION_INVALID_DETAIL)


def _hydrate_motoboy_permissions_from_db(
    db: Session,
    user: User,
    *,
    response: Optional[Response] = None,
    jwt_claims_version: Any = None,
) -> User:
    """Sobrescreve flags do JWT com valores vivos (Motoboy + Owner)."""
    uid = getattr(user, "id", None)
    if uid is None:
        return user
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        return user

    motoboy = run_db_query_with_retry(
        db,
        lambda: db.scalar(select(Motoboy).where(Motoboy.user_id == uid_int)),
    )
    if not motoboy:
        return user

    sub_base = (getattr(user, "sub_base", None) or getattr(motoboy, "sub_base", None) or "").strip()
    owner = None
    if sub_base:
        owner = run_db_query_with_retry(
            db,
            lambda: db.scalar(select(Owner).where(Owner.sub_base == sub_base)),
        )

    pode_realizar = bool(getattr(motoboy, "pode_realizar_coleta", getattr(motoboy, "pode_ler_coleta", False)))
    modo = "codigo"
    ignorar = False
    if owner is not None:
        ignorar = bool(owner.ignorar_coleta)
        modo = (getattr(owner, "modo_operacao", None) or "codigo").strip().lower()
        user.ignorar_coleta = ignorar
        user.modo_operacao = modo
        user.tipo_owner = _tipo_owner_from_owner(owner)
        user.owner_valor = Decimal(str(owner.valor or 0))
        user.devolucao_sub_base_habilitada = bool(getattr(owner, "devolucao_sub_base_habilitada", False))
        user.entrada_obrigatoria_habilitada = bool(getattr(owner, "entrada_obrigatoria_habilitada", False))
        user.conferencia_saida_habilitada = bool(getattr(owner, "conferencia_saida_habilitada", False))
        user.bloquear_saida_sem_coleta = bool(getattr(owner, "bloquear_saida_sem_coleta", False))
        user.owner_ativo = bool(owner.ativo)
        user.sub_base_nome = (owner.sub_base or sub_base).strip() or sub_base

    pode_ler_coleta = pode_realizar and modo in ("codigo", "ambos")
    if ignorar:
        pode_ler_coleta = False
        pode_realizar = False

    user.motoboy_id = int(motoboy.id_motoboy)
    user.pode_realizar_coleta = pode_realizar
    user.pode_ler_coleta = bool(pode_ler_coleta)
    user.pode_ler_saida = bool(getattr(motoboy, "pode_ler_saida", True))
    user.pode_digitar_codigo_manual = bool(getattr(motoboy, "pode_digitar_codigo_manual", False))
    user.pode_lancar_avulso = bool(getattr(motoboy, "pode_lancar_avulso", True))
    user.avulso_exige_foto = bool(getattr(motoboy, "avulso_exige_foto", True))
    live_version = int(getattr(motoboy, "claims_version", 0) or 0)
    user.claims_version = live_version

    try:
        jwt_ver = int(jwt_claims_version) if jwt_claims_version is not None and jwt_claims_version != "" else None
    except (TypeError, ValueError):
        jwt_ver = None
    if response is not None and jwt_ver is not None and jwt_ver != live_version:
        response.headers[CLAIMS_STALE_HEADER] = "1"

    return user


# ======================================================
# Usuário logado — JWT; staff revalida role; motoboy hidrata flags
# ======================================================
async def get_current_user(
    request: Request,
    response: Response,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
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

    role_int = _coerce_role_int(payload.get("role"))

    if not payload.get("owner_ativo", False):
        if role_int != 4:
            raise HTTPException(status_code=403, detail="Operação bloqueada")

    if role_int in (0, 1, 2, 3):
        _ensure_staff_jwt_matches_db(db, uid=payload.get("uid"), jwt_role=role_int)

    request.state.ignorar_coleta = payload.get("ignorar_coleta", False)
    user = _user_from_claims(payload)

    if role_int == 4:
        user = _hydrate_motoboy_permissions_from_db(
            db,
            user,
            response=response,
            jwt_claims_version=payload.get("claims_version"),
        )
        request.state.ignorar_coleta = bool(getattr(user, "ignorar_coleta", False))

    return user


# ======================================================
# ROTAS
# ======================================================
@router.post("/token")
async def login_for_access_token(
    user_credentials: UserLogin,
    db: Session = Depends(get_db),
):
    """
    Login staff (admin/operador/root) para API/mobile.
    Com remember=true (app mobile / “lembrar”), emite access longo como o cookie web.
    Sem remember, mantém TTL curto (ACCESS_TOKEN_EXPIRE_MINUTES).
    Root (role=0): retorna needs_sub_base_selection + lista de bases (sem token).
    """
    user = authenticate_user(db, user_credentials.identifier, user_credentials.password)
    if not user:
        raise HTTPException(401, "Login ou senha incorretos")

    if user.role == 0:
        return _root_needs_sub_base_response(db, user)

    # Motoboy não pode receber JWT staff (sem motoboy_id) — quebra /mobile/*
    if int(user.role or 0) == 4:
        raise HTTPException(
            403,
            "Conta de entregador: use o login de motoboy.",
        )

    if not user.sub_base:
        raise HTTPException(403, "Usuário sem sub_base definida")

    owner = _owner_for_sub_base(db, user.sub_base)
    expires = _staff_expires(user_credentials.remember)
    issued = _issue_staff_auth(user, owner, expires)
    return {
        "access_token": issued["access_token"],
        "token_type": "bearer",
        "must_change_password": issued["must_change_password"],
        "expires_in": issued["expires_in"],
    }


@router.post("/login")
async def login_set_cookie(
    user_credentials: UserLogin,
    response: Response,
    db: Session = Depends(get_db),
):
    user = authenticate_user(db, user_credentials.identifier, user_credentials.password)
    if not user:
        raise HTTPException(401, "Login ou senha incorretos")

    if user.role == 0:
        return _root_needs_sub_base_response(db, user)

    if int(user.role or 0) == 4:
        raise HTTPException(
            403,
            "Conta de entregador: use o login de motoboy.",
        )

    if not user.sub_base:
        raise HTTPException(403, "Usuário sem sub_base definida")

    owner = _owner_for_sub_base(db, user.sub_base)
    expires = _staff_expires(user_credentials.remember)
    issued = _issue_staff_auth(user, owner, expires, response=response)
    return {
        "ok": True,
        "user": issued["user"],
    }


@router.post("/root-select-subbase")
async def root_select_subbase(
    body: RootSelectSubBase,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    Após /login ou /token com needs_sub_base_selection (role=0),
    envia identifier + password + sub_base para obter sessão na base escolhida.
    Retorna access_token (mobile) e seta cookie HttpOnly (web).
    """
    meta = _auth_attempt_meta(body.identifier)
    selected_sub_base = body.sub_base.strip()
    logger.info(
        "root_select_subbase_attempt attempt_id=%s identifier_mask=%s identifier_hash=%s sub_base=%s",
        meta["attempt_id"], meta["identifier_mask"], meta["identifier_hash"], selected_sub_base,
    )

    user = authenticate_user(db, body.identifier, body.password)
    if not user:
        logger.warning(
            "root_select_subbase_failed attempt_id=%s reason=invalid_credentials",
            meta["attempt_id"],
        )
        raise HTTPException(401, "Login ou senha incorretos")

    if user.role != 0:
        logger.info(
            "root_select_subbase_failed attempt_id=%s reason=non_root_role user_id=%s role=%s",
            meta["attempt_id"], user.id, user.role,
        )
        raise HTTPException(403, "Acesso restrito a root.")

    owner = _owner_for_sub_base(db, selected_sub_base)

    # Persistir a base escolhida no cadastro do root para consultas que leem do banco
    if (user.sub_base or "").strip() != selected_sub_base:
        user.sub_base = selected_sub_base
        db.add(user)
        db.commit()
        db.refresh(user)

    expires = _staff_expires(body.remember)
    issued = _issue_staff_auth(
        user, owner, expires, sub_base=selected_sub_base, response=response,
    )
    logger.info(
        "root_select_subbase_success attempt_id=%s user_id=%s sub_base=%s",
        meta["attempt_id"], user.id, selected_sub_base,
    )
    return {
        "access_token": issued["access_token"],
        "token_type": "bearer",
        "must_change_password": issued["must_change_password"],
        "expires_in": issued["expires_in"],
        "ok": True,
        "user": issued["user"],
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

def _default_password_motoboy(sub_base: Optional[str]) -> str:
    """Senha padrão motoboy: {subbase}_trocar_senha em minúsculo (ex.: Giro Express -> giroexpress_trocar_senha)."""
    norm = (sub_base or "").strip().replace(" ", "").lower()
    return f"{norm}_trocar_senha" if norm else "migrado_trocar_senha"


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
    meta = _auth_attempt_meta(body.identifier)
    logger.info(
        "motoboy_login_attempt attempt_id=%s identifier_mask=%s identifier_hash=%s",
        meta["attempt_id"], meta["identifier_mask"], meta["identifier_hash"]
    )

    user = authenticate_user(db, body.identifier, body.password)
    if not user:
        logger.warning(
            "motoboy_login_failed attempt_id=%s reason=invalid_credentials",
            meta["attempt_id"]
        )
        raise HTTPException(401, "Login ou senha incorretos")

    if user.role != 4:
        logger.info(
            "motoboy_login_fallback_candidate attempt_id=%s reason=non_motoboy_role user_id=%s role=%s",
            meta["attempt_id"], user.id, user.role
        )
        raise HTTPException(403, "Acesso restrito a motoboys.")

    motoboy = run_db_query_with_retry(
        db,
        lambda: db.scalar(select(Motoboy).where(Motoboy.user_id == user.id)),
    )
    if not motoboy:
        logger.warning(
            "motoboy_login_failed attempt_id=%s reason=motoboy_profile_not_found user_id=%s",
            meta["attempt_id"], user.id
        )
        raise HTTPException(404, "Perfil de motoboy não encontrado.")

    sub_bases_rows = run_db_query_with_retry(
        db,
        lambda: db.scalars(
            select(MotoboySubBase.sub_base).where(
                MotoboySubBase.motoboy_id == motoboy.id_motoboy,
                MotoboySubBase.ativo.is_(True),
            )
        ).all(),
    )
    sub_bases = [s for s in sub_bases_rows if s]

    # Política de troca obrigatória de senha baseada em flag persistido + senha padrão
    must_change_password = _must_change_password_from_user(user)

    if len(sub_bases) > 1:
        logger.info(
            "motoboy_login_multiple_sub_base attempt_id=%s user_id=%s motoboy_id=%s sub_base_count=%s must_change_password=%s",
            meta["attempt_id"], user.id, motoboy.id_motoboy, len(sub_bases), must_change_password
        )
        return {"multiple_sub_base": True, "sub_bases": sub_bases, "must_change_password": must_change_password}

    if len(sub_bases) == 0:
        logger.warning(
            "motoboy_login_failed attempt_id=%s reason=no_active_sub_base user_id=%s motoboy_id=%s",
            meta["attempt_id"], user.id, motoboy.id_motoboy
        )
        raise HTTPException(403, "Motoboy sem sub_base ativa vinculada.")

    sub_base = sub_bases[0]
    try:
        owner = _owner_for_sub_base(db, sub_base)
    except HTTPException as exc:
        logger.warning(
            "motoboy_login_failed attempt_id=%s reason=owner_validation_error sub_base=%s status=%s detail=%s",
            meta["attempt_id"], sub_base, exc.status_code, exc.detail
        )
        raise
    logger.info(
        "motoboy_login_success attempt_id=%s user_id=%s motoboy_id=%s sub_base=%s must_change_password=%s",
        meta["attempt_id"], user.id, motoboy.id_motoboy, sub_base, must_change_password
    )
    return _issue_motoboy_auth_response(db, user, motoboy, owner, sub_base)


@router.post("/motoboy-select-subbase", response_model=Token)
async def motoboy_select_subbase(
    body: MotoboySelectSubBase,
    db: Session = Depends(get_db),
):
    """
    Após motoboy-login com multiple_sub_base, o app envia
    identifier + password + sub_base escolhida para obter o token.
    """
    meta = _auth_attempt_meta(body.identifier)
    selected_sub_base = body.sub_base.strip()
    logger.info(
        "motoboy_select_subbase_attempt attempt_id=%s identifier_mask=%s identifier_hash=%s sub_base=%s",
        meta["attempt_id"], meta["identifier_mask"], meta["identifier_hash"], selected_sub_base
    )

    user = authenticate_user(db, body.identifier, body.password)
    if not user:
        logger.warning(
            "motoboy_select_subbase_failed attempt_id=%s reason=invalid_credentials",
            meta["attempt_id"]
        )
        raise HTTPException(401, "Login ou senha incorretos")

    if user.role != 4:
        logger.info(
            "motoboy_select_subbase_fallback_candidate attempt_id=%s reason=non_motoboy_role user_id=%s role=%s",
            meta["attempt_id"], user.id, user.role
        )
        raise HTTPException(403, "Acesso restrito a motoboys.")

    motoboy = run_db_query_with_retry(
        db,
        lambda: db.scalar(select(Motoboy).where(Motoboy.user_id == user.id)),
    )
    if not motoboy:
        logger.warning(
            "motoboy_select_subbase_failed attempt_id=%s reason=motoboy_profile_not_found user_id=%s",
            meta["attempt_id"], user.id
        )
        raise HTTPException(404, "Perfil de motoboy não encontrado.")

    existe = run_db_query_with_retry(
        db,
        lambda: db.scalar(
            select(MotoboySubBase).where(
                MotoboySubBase.motoboy_id == motoboy.id_motoboy,
                MotoboySubBase.sub_base == selected_sub_base,
                MotoboySubBase.ativo.is_(True),
            )
        ),
    )
    if not existe:
        logger.warning(
            "motoboy_select_subbase_failed attempt_id=%s reason=invalid_or_inactive_sub_base user_id=%s motoboy_id=%s sub_base=%s",
            meta["attempt_id"], user.id, motoboy.id_motoboy, selected_sub_base
        )
        raise HTTPException(400, "Sub_base inválida ou inativa para este motoboy.")

    try:
        owner = _owner_for_sub_base(db, selected_sub_base)
    except HTTPException as exc:
        logger.warning(
            "motoboy_select_subbase_failed attempt_id=%s reason=owner_validation_error sub_base=%s status=%s detail=%s",
            meta["attempt_id"], selected_sub_base, exc.status_code, exc.detail
        )
        raise
    logger.info(
        "motoboy_select_subbase_success attempt_id=%s user_id=%s motoboy_id=%s sub_base=%s must_change_password=%s",
        meta["attempt_id"], user.id, motoboy.id_motoboy, selected_sub_base, _must_change_password_from_user(user)
    )
    return _issue_motoboy_auth_response(db, user, motoboy, owner, selected_sub_base)


@router.post("/motoboy-refresh", response_model=Token)
async def motoboy_refresh(body: MotoboyRefreshBody, db: Session = Depends(get_db)):
    """Renova access token usando refresh token (app mobile)."""
    return _rotate_motoboy_refresh_token(db, body.refresh_token)


@router.post("/motoboy-logout")
async def motoboy_logout(body: MotoboyLogoutBody, db: Session = Depends(get_db)):
    """Revoga refresh token do motoboy (logout manual app mobile)."""
    if body.refresh_token:
        token_hash = _hash_refresh_token(body.refresh_token.strip())

        def _revoke_token() -> None:
            row = db.scalar(
                select(MotoboyRefreshToken).where(
                    MotoboyRefreshToken.token_hash == token_hash,
                    MotoboyRefreshToken.revoked_at.is_(None),
                )
            )
            if row:
                row.revoked_at = datetime.utcnow()
                db.commit()

        run_db_query_with_retry(db, _revoke_token)
    return {"ok": True}


def _nome_exibicao(user: User) -> tuple[Optional[str], Optional[str]]:
    """Retorna (nome, sobrenome) para exibição. Quando ambos vazios, deriva do username."""
    from name_normalizer import normalize_person_name

    nome_val = (getattr(user, "nome", None) or "").strip()
    sobrenome_val = (getattr(user, "sobrenome", None) or "").strip()
    if not nome_val and not sobrenome_val and (user.username or "").strip():
        # Fallback: formata username como nome (ex: joao.silva -> Joao Silva)
        partes = (user.username or "").replace(".", " ").replace("_", " ").split()
        nome_val = " ".join(partes) if partes else ""
    return (
        normalize_person_name(nome_val),
        normalize_person_name(sobrenome_val),
    )


@router.get("/me", response_model=UserResponse)
async def read_users_me(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_user = run_db_query_with_retry(db, lambda: db.get(User, current_user.id))
    if not db_user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    if not bool(getattr(db_user, "status", True)):
        raise HTTPException(status_code=401, detail=SESSION_INVALID_DETAIL)

    live_role = _coerce_role_int(getattr(db_user, "role", None))
    jwt_role = _coerce_role_int(getattr(current_user, "role", None))
    if live_role != jwt_role:
        raise HTTPException(status_code=401, detail=SESSION_INVALID_DETAIL)

    nome_val, sobrenome_val = _nome_exibicao(current_user)
    # tipo_owner vivo do Owner (não só do JWT) — sessão mobile longa pode ficar desatualizada
    # Preferir sub_base da sessão (JWT), essencial para root com base selecionada no login
    tipo_owner = getattr(current_user, "tipo_owner", None) or "subbase"
    ignorar_coleta = bool(getattr(request.state, "ignorar_coleta", False))
    modo_operacao = getattr(current_user, "modo_operacao", None) or "codigo"
    entrada_obrigatoria = bool(getattr(current_user, "entrada_obrigatoria_habilitada", False))
    conferencia = bool(getattr(current_user, "conferencia_saida_habilitada", False))
    bloquear_saida_sem_coleta = bool(getattr(current_user, "bloquear_saida_sem_coleta", False))
    sub_base = (
        getattr(current_user, "sub_base", None)
        or getattr(db_user, "sub_base", None)
        or ""
    ).strip()
    if sub_base:
        owner = run_db_query_with_retry(
            db,
            lambda: db.scalar(select(Owner).where(Owner.sub_base == sub_base)),
        )
        if owner is not None:
            tipo_owner = _tipo_owner_from_owner(owner)
            ignorar_coleta = bool(owner.ignorar_coleta)
            modo_operacao = (getattr(owner, "modo_operacao", None) or "codigo")
            entrada_obrigatoria = bool(getattr(owner, "entrada_obrigatoria_habilitada", False))
            conferencia = bool(getattr(owner, "conferencia_saida_habilitada", False))
            bloquear_saida_sem_coleta = bool(getattr(owner, "bloquear_saida_sem_coleta", False))
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        username=current_user.username,
        nome=nome_val,
        sobrenome=sobrenome_val,
        contato=current_user.contato,
        role=live_role,
        sub_base=sub_base or current_user.sub_base,
        ignorar_coleta=ignorar_coleta,
        modo_operacao=modo_operacao,
        tipo_owner=tipo_owner,
        must_change_password=bool(getattr(db_user, "must_change_password", False)),
        entrada_obrigatoria_habilitada=entrada_obrigatoria,
        conferencia_saida_habilitada=conferencia,
        bloquear_saida_sem_coleta=bloquear_saida_sem_coleta,
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
    # Reset de senha via identifier define uma nova senha definitiva; não exige troca imediata
    if hasattr(user, "must_change_password"):
        user.must_change_password = False
    db.commit()

    return {"ok": True, "message": "Senha redefinida com sucesso"}
