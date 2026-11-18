"""PRD-011: helpers de papel do cliente com acesso ao banco."""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from base_papel_pure import exige_papel_obrigatorio, papel_ao_promover_owner_para_base
from models import BasePreco, Owner


def tipo_owner_da_sub_base(db: Session, sub_base: str) -> str:
    sb = (sub_base or "").strip()
    owners = list(
        db.scalars(
            select(Owner).where(func.btrim(func.coalesce(Owner.sub_base, "")) == sb)
        ).all()
    )
    if len(owners) != 1:
        return "subbase"
    return (getattr(owners[0], "tipo_owner", None) or "subbase").strip().lower()


def resolver_papel_ao_criar(db: Session, sub_base: str, papel: Optional[str]) -> Optional[str]:
    tipo = tipo_owner_da_sub_base(db, sub_base)
    if exige_papel_obrigatorio(tipo):
        if not papel:
            raise HTTPException(status_code=422, detail="Informe o papel do cliente: Seller ou Base.")
        return papel
    return None


def papel_default_importacao(db: Session, sub_base: str) -> Optional[str]:
    if exige_papel_obrigatorio(tipo_owner_da_sub_base(db, sub_base)):
        return "seller"
    return None


def preencher_papel_base_parceira_ao_promover(db: Session, sub_base: Optional[str]) -> int:
    """Ao promover owner para tipo base, clientes sem papel recebem base_parceira."""
    sb = (sub_base or "").strip()
    if not sb:
        return 0
    owners = list(
        db.scalars(select(Owner).where(func.btrim(func.coalesce(Owner.sub_base, "")) == sb)).all()
    )
    papel = papel_ao_promover_owner_para_base(
        qtd_owners=len(owners),
        tipo_owner=getattr(owners[0], "tipo_owner", None) if owners else None,
    )
    if not papel:
        return 0
    rows = list(
        db.scalars(
            select(BasePreco).where(
                func.btrim(func.coalesce(BasePreco.sub_base, "")) == sb,
                BasePreco.papel.is_(None),
            )
        ).all()
    )
    for row in rows:
        row.papel = papel
    return len(rows)
