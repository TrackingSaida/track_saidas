from __future__ import annotations

from fastapi import Depends, HTTPException, status
from typing import Callable

from models import User

# IMPORTAÇÕES QUE ESTAVAM FALTANDO
from auth import (
    get_current_user as auth_get_current_user,
    oauth2_scheme,
    SECRET_KEY,
    ALGORITHM,
)
from jose import jwt


def _coerce_role_from_user(user: User) -> int:
    """
    Extrai o 'role' a partir de user.role.
    Defaults seguro: 3 (mais restrito) se vier None/0/inválido.
    """
    try:
        value = int(getattr(user, "role", 3) or 3)
        if value not in (1, 2, 3):
            return 3
        return value
    except Exception:
        return 3


def current_user_with_role(user: User = Depends(auth_get_current_user)) -> User:
    """
    Wrap no current_user do auth.py:
    - mantém autenticação (cookie/Bearer) e busca no DB
    - anexa 'user.role' com base em 'role'
    """
    user.role = _coerce_role_from_user(user)
    return user


def allow(*tipos_permitidos: int) -> Callable:
    """
    Guard (RBAC) para usar nas rotas.
    """
    permitidos = tuple(x for x in tipos_permitidos if x in (1, 2, 3))
    if not permitidos:
        permitidos = (0,)

    def guard(user: User = Depends(current_user_with_role)) -> User:
        if int(user.role) not in permitidos:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acesso negado",
            )
        return user

    return guard


# ======================================================
# TOKEN DO ENTREGADOR (NOVO FLUXO)
# ======================================================
def get_entregador_id_from_token(token: str = Depends(oauth2_scheme)):
    """
    Extrai o entregador_id do token JWT.
    Apenas tokens cujo sub == 'entregador' são aceitos.
    """

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

        if payload.get("sub") != "entregador":
            raise HTTPException(403, "Token não é de entregador")

        return payload["entregador_id"]

    except Exception:
        raise HTTPException(401, "Token inválido")
