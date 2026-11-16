"""PRD-011: regras puras do papel do cliente (seller | base_parceira)."""
from __future__ import annotations

from typing import Optional

PAPEIS = frozenset({"seller", "base_parceira"})


def normalizar_papel(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    if v not in PAPEIS:
        raise ValueError("papel deve ser seller ou base_parceira.")
    return v


def exige_papel_obrigatorio(tipo_owner: Optional[str]) -> bool:
    return (tipo_owner or "").strip().lower() == "base"


def eh_seller(papel: Optional[str]) -> bool:
    return (papel or "").strip().lower() == "seller"


def eh_base_parceira(papel: Optional[str]) -> bool:
    return (papel or "").strip().lower() == "base_parceira"


def permite_autenticacao_portal(papel: Optional[str], tipo_owner: Optional[str]) -> bool:
    """Owner subbase: todos os clientes. Owner base: só seller."""
    if not exige_papel_obrigatorio(tipo_owner):
        return True
    return eh_seller(papel)
