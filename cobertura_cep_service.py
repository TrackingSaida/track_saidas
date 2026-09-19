"""Área de cobertura por prefixo de CEP (sub_base)."""
from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import CoberturaCepPrefixo

_DIGITS_RE = re.compile(r"\D+")


def normalize_cep_digits(cep: Optional[str]) -> str:
    return _DIGITS_RE.sub("", cep or "")


def normalize_prefixo(prefixo: Optional[str]) -> str:
    return normalize_cep_digits(prefixo)


def list_prefixos_ativos(db: Session, sub_base: str) -> List[str]:
    sub = (sub_base or "").strip()
    if not sub:
        return []
    rows = db.scalars(
        select(CoberturaCepPrefixo.prefixo)
        .where(
            CoberturaCepPrefixo.sub_base == sub,
            CoberturaCepPrefixo.ativo.is_(True),
        )
        .order_by(CoberturaCepPrefixo.prefixo)
    ).all()
    return [normalize_prefixo(p) for p in rows if normalize_prefixo(p)]


def replace_prefixos(db: Session, sub_base: str, prefixos: Sequence[str]) -> List[str]:
    sub = (sub_base or "").strip()
    cleaned: List[str] = []
    seen = set()
    for raw in prefixos or []:
        p = normalize_prefixo(raw)
        if not p or p in seen:
            continue
        seen.add(p)
        cleaned.append(p)

    existing = list(
        db.scalars(select(CoberturaCepPrefixo).where(CoberturaCepPrefixo.sub_base == sub)).all()
    )
    by_prefix = {normalize_prefixo(r.prefixo): r for r in existing}
    keep = set(cleaned)
    for p, row in by_prefix.items():
        if p not in keep:
            db.delete(row)
    for p in cleaned:
        row = by_prefix.get(p)
        if row:
            row.ativo = True
            row.prefixo = p
        else:
            db.add(CoberturaCepPrefixo(sub_base=sub, prefixo=p, ativo=True))
    return cleaned


def cep_coberto(cep: Optional[str], prefixos: Sequence[str]) -> bool:
    if not prefixos:
        return True
    digits = normalize_cep_digits(cep)
    if not digits:
        return False
    return any(digits.startswith(p) for p in prefixos if p)


def avaliar_cobertura(
    db: Session, sub_base: str, cep: Optional[str]
) -> Tuple[bool, List[str]]:
    prefixos = list_prefixos_ativos(db, sub_base)
    return cep_coberto(cep, prefixos), prefixos
