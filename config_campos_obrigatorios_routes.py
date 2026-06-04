from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_current_user
from db import get_db
from codigo_normalizer import canonicalize_servico
from models import PedidoCamposObrigatoriosConfig, User
from pedido_campos_obrigatorios_service import (
    CONTEXTOS_VALIDOS,
    CAMPOS_VALIDOS,
    normalize_contexto,
    normalize_campos_obrigatorios,
)

router = APIRouter(prefix="/configuracoes/campos-obrigatorios-pedido", tags=["Configuração - Campos Obrigatórios"])
SERVICOS_DISPONIVEIS = ["Shopee", "Mercado Livre", "Avulso"]


class CamposObrigatoriosRuleIn(BaseModel):
    servico: str = Field(min_length=1)
    contexto: str = Field(min_length=1)
    campos_obrigatorios: List[str] = Field(default_factory=list)
    ativo: bool = True


class CamposObrigatoriosRuleOut(BaseModel):
    id: int
    sub_base: str
    servico: str
    contexto: str
    campos_obrigatorios: List[str]
    ativo: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def _assert_admin_root(current_user: User) -> None:
    role = int(getattr(current_user, "role", 0) or 0)
    if role not in (0, 1):
        raise HTTPException(status_code=403, detail="Acesso restrito a admin/root.")


def _row_to_out(row: PedidoCamposObrigatoriosConfig) -> CamposObrigatoriosRuleOut:
    try:
        campos = json.loads((row.campos_obrigatorios or "[]").strip() or "[]")
        if not isinstance(campos, list):
            campos = []
    except Exception:
        campos = []
    return CamposObrigatoriosRuleOut(
        id=int(row.id),
        sub_base=row.sub_base,
        servico=row.servico,
        contexto=row.contexto,
        campos_obrigatorios=[str(c) for c in campos],
        ativo=bool(row.ativo),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/meta")
def get_campos_obrigatorios_meta(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin_root(current_user)
    return {
        "servicos": SERVICOS_DISPONIVEIS,
        "contextos": sorted(CONTEXTOS_VALIDOS),
        "campos": sorted(CAMPOS_VALIDOS),
    }


@router.get("", response_model=List[CamposObrigatoriosRuleOut])
def list_campos_obrigatorios_rules(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin_root(current_user)
    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário sem sub_base.")
    rows = db.scalars(
        select(PedidoCamposObrigatoriosConfig)
        .where(PedidoCamposObrigatoriosConfig.sub_base == sub_base)
        .order_by(PedidoCamposObrigatoriosConfig.id.desc())
    ).all()
    return [_row_to_out(r) for r in rows]


@router.post("", response_model=CamposObrigatoriosRuleOut, status_code=201)
def create_campos_obrigatorios_rule(
    body: CamposObrigatoriosRuleIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin_root(current_user)
    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário sem sub_base.")

    row = PedidoCamposObrigatoriosConfig(
        sub_base=sub_base,
        servico=canonicalize_servico(body.servico),
        contexto=normalize_contexto(body.contexto),
        campos_obrigatorios=json.dumps(normalize_campos_obrigatorios(body.campos_obrigatorios)),
        ativo=bool(body.ativo),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row_to_out(row)


@router.put("/{rule_id}", response_model=CamposObrigatoriosRuleOut)
def update_campos_obrigatorios_rule(
    rule_id: int,
    body: CamposObrigatoriosRuleIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin_root(current_user)
    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário sem sub_base.")

    row = db.get(PedidoCamposObrigatoriosConfig, rule_id)
    if not row or row.sub_base != sub_base:
        raise HTTPException(status_code=404, detail="Regra não encontrada.")

    row.servico = canonicalize_servico(body.servico)
    row.contexto = normalize_contexto(body.contexto)
    row.campos_obrigatorios = json.dumps(normalize_campos_obrigatorios(body.campos_obrigatorios))
    row.ativo = bool(body.ativo)
    db.commit()
    db.refresh(row)
    return _row_to_out(row)


@router.delete("/{rule_id}", status_code=204)
def delete_campos_obrigatorios_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin_root(current_user)
    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário sem sub_base.")

    row = db.get(PedidoCamposObrigatoriosConfig, rule_id)
    if not row or row.sub_base != sub_base:
        raise HTTPException(status_code=404, detail="Regra não encontrada.")
    db.delete(row)
    db.commit()
    return None
