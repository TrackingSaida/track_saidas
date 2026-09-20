"""Auth JWT isolada do portal do seller (não usa roles de users)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import ALGORITHM, SECRET_KEY, create_access_token, verify_password
from db import get_db
from models import Owner, SellerPortalAccess

security = HTTPBearer(auto_error=False)
SELLER_TOKEN_TYPE = "seller"
SELLER_ACCESS_EXPIRE_HOURS = 12


class SellerContext:
    def __init__(self, access: SellerPortalAccess, owner: Owner):
        self.access = access
        self.owner = owner
        self.sub_base = (access.sub_base or "").strip()
        self.id_base = int(access.id_base)
        self.login = access.login
        self.must_change_password = bool(access.must_change_password)


def issue_seller_token(access: SellerPortalAccess) -> str:
    claims = {
        "typ": SELLER_TOKEN_TYPE,
        "sid": int(access.id),
        "sub_base": (access.sub_base or "").strip(),
        "id_base": int(access.id_base),
        "login": access.login,
    }
    return create_access_token(claims, timedelta(hours=SELLER_ACCESS_EXPIRE_HOURS))


def get_current_seller(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> SellerContext:
    token: Optional[str] = None
    if credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    if not token:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Não autenticado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")
    if payload.get("typ") != SELLER_TOKEN_TYPE:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")
    sid = payload.get("sid")
    try:
        sid_int = int(sid)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")
    access = db.get(SellerPortalAccess, sid_int)
    if not access or not access.ativo:
        raise HTTPException(status_code=401, detail="Acesso desativado. Fale com a operação.")
    owner = db.scalar(select(Owner).where(Owner.sub_base == (access.sub_base or "").strip()))
    if not owner or not owner.ativo:
        raise HTTPException(status_code=403, detail="Operação bloqueada")
    return SellerContext(access, owner)


def authenticate_seller(db: Session, login: str, password: str) -> SellerPortalAccess:
    ident = (login or "").strip()
    if not ident:
        raise HTTPException(401, "Login ou senha incorretos")
    access = db.scalar(select(SellerPortalAccess).where(SellerPortalAccess.login == ident))
    if not access or not access.ativo:
        raise HTTPException(401, "Login ou senha incorretos")
    if not verify_password(password, access.password_hash):
        raise HTTPException(401, "Login ou senha incorretos")
    access.last_login_at = datetime.utcnow()
    db.commit()
    db.refresh(access)
    return access
