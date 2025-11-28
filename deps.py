# deps.py
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from typing import Callable

from models import User
from auth import get_current_user as auth_get_current_user  # reutiliza seu auth.get_current_user


def _coerce_role_from_user(user: User) -> int:
    """
    Extrai o 'role' a partir de user.role.
    Defaults seguro: 3 (mais restrito) se vier None/0/inválido.
    """
    try:
        value = int(getattr(user, "role", 3) or 3)
        # garante faixa conhecida (1,2,3); se sair disso, cai para 3
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
    Ex.: Operação -> Depends(allow(1,2,3))
         Config   -> Depends(allow(1,2))
         Dash     -> Depends(allow(1))
    """
    # normaliza lista e filtra valores válidos
    permitidos = tuple(x for x in tipos_permitidos if x in (1, 2, 3))
    if not permitidos:
        # fallback seguro: ninguém acessa, caso alguém use allow() sem argumentos
        permitidos = (0,)

    def guard(user: User = Depends(current_user_with_role)) -> User:
        if int(user.role) not in permitidos:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acesso negado",
            )
        return user

    return guard


def get_entregador_id_from_token(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") != "entregador":
            raise HTTPException(403, "Token não é de entregador")
        return payload["entregador_id"]
    except:
        raise HTTPException(401, "Token inválido")



# --------- EXEMPLOS DE USO (para referência) ----------
# from fastapi import APIRouter, Depends
# router = APIRouter()
#
# @router.get("/operacao/registros")        # 1,2,3
# def listar_registros(user = Depends(allow(1,2,3))):
#     ...
#
# @router.get("/config/entregadores")       # 1,2
# def cfg_entregadores(user = Depends(allow(1,2))):
#     ...
#
# @router.get("/dashboards/tracking")       # 1
# def dash_tracking(user = Depends(allow(1))):
#     ...
