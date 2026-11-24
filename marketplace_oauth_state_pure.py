"""Encode/decode do state OAuth marketplace (sem Session/models)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from jose import JWTError, jwt

STATE_TYP = "mkt_oauth"
STATE_TTL = timedelta(hours=48)
_ALGORITHM = "HS256"


def _secret() -> str:
    key = os.getenv("SECRET_KEY")
    if not key:
        raise RuntimeError("SECRET_KEY não configurada no ambiente")
    return key


def encode_oauth_state(sub_base: str, id_base: Optional[int] = None) -> str:
    """Sem id_base devolve sub_base puro (compat). Com id_base devolve JWT assinado."""
    sb = (sub_base or "").strip()
    if id_base is None:
        return sb
    now = datetime.now(timezone.utc)
    payload = {
        "typ": STATE_TYP,
        "sb": sb,
        "ib": int(id_base),
        "iat": now,
        "exp": now + STATE_TTL,
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def decode_oauth_state(state: Optional[str]) -> Tuple[str, Optional[int]]:
    """Retorna (sub_base, id_base|None). JWT inválido/expirado → sub_base vazio."""
    raw = (state or "").strip()
    if not raw:
        return "", None
    if raw.count(".") >= 2:
        try:
            payload = jwt.decode(raw, _secret(), algorithms=[_ALGORITHM])
        except JWTError:
            return "", None
        if payload.get("typ") != STATE_TYP:
            return "", None
        sb = str(payload.get("sb") or "").strip()
        ib = payload.get("ib")
        try:
            id_base = int(ib) if ib is not None else None
        except (TypeError, ValueError):
            id_base = None
        return sb, id_base
    return raw, None
