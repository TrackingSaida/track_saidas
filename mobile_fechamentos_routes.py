"""
Fechamentos do motoboy autenticado (lista, detalhe, PDF).
Prefixo: /mobile/fechamentos
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_current_user
from db import get_db
from fechamento_pdf_service import get_fechamento_pdf_bytes, build_fechamento_code
from models import EntregadorFechamento, Motoboy, User
from entregador_fechamento_routes import _get_motoboy_chave_pix

router = APIRouter(prefix="/mobile/fechamentos", tags=["Mobile Fechamentos"])


def _require_motoboy(user: User) -> tuple[int, str]:
    try:
        role = int(getattr(user, "role", 0) or 0)
    except (TypeError, ValueError):
        role = 0
    mid = getattr(user, "motoboy_id", None)
    sub = (getattr(user, "sub_base", None) or "").strip()
    if role != 4 or not mid or not sub:
        raise HTTPException(403, "Acesso restrito a motoboy.")
    return int(mid), sub


class FechamentoMobileOut(BaseModel):
    id_fechamento: int
    codigo: str
    periodo_inicio: date
    periodo_fim: date
    valor_base: Decimal
    valor_adicao: Decimal
    valor_subtracao: Decimal
    valor_final: Decimal
    motivo_adicao: Optional[str] = None
    motivo_subtracao: Optional[str] = None
    status: str
    chave_pix: Optional[str] = None
    criado_em: Optional[datetime] = None
    tem_pdf: bool = False


def _to_out(fech: EntregadorFechamento, chave_pix: Optional[str]) -> FechamentoMobileOut:
    return FechamentoMobileOut(
        id_fechamento=fech.id_fechamento,
        codigo=build_fechamento_code(fech),
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_base=fech.valor_base,
        valor_adicao=fech.valor_adicao,
        valor_subtracao=fech.valor_subtracao,
        valor_final=fech.valor_final,
        motivo_adicao=fech.motivo_adicao,
        motivo_subtracao=fech.motivo_subtracao,
        status=fech.status,
        chave_pix=chave_pix,
        criado_em=fech.criado_em,
        tem_pdf=bool((fech.pdf_object_key or "").strip()),
    )


@router.get("", response_model=List[FechamentoMobileOut])
def listar_fechamentos(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    motoboy_id, sub_base = _require_motoboy(current_user)
    rows = db.scalars(
        select(EntregadorFechamento)
        .where(
            EntregadorFechamento.sub_base == sub_base,
            EntregadorFechamento.id_motoboy == motoboy_id,
        )
        .order_by(EntregadorFechamento.periodo_fim.desc(), EntregadorFechamento.id_fechamento.desc())
        .limit(50)
    ).all()
    chave = _get_motoboy_chave_pix(db, motoboy_id)
    return [_to_out(r, chave) for r in rows]


@router.get("/{id_fechamento}", response_model=FechamentoMobileOut)
def detalhe_fechamento(
    id_fechamento: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    motoboy_id, sub_base = _require_motoboy(current_user)
    fech = db.get(EntregadorFechamento, id_fechamento)
    if (
        not fech
        or fech.sub_base != sub_base
        or fech.id_motoboy != motoboy_id
    ):
        raise HTTPException(404, "Fechamento não encontrado.")
    chave = _get_motoboy_chave_pix(db, motoboy_id)
    return _to_out(fech, chave)


@router.get("/{id_fechamento}/pdf")
def baixar_pdf_fechamento(
    id_fechamento: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    motoboy_id, sub_base = _require_motoboy(current_user)
    fech = db.get(EntregadorFechamento, id_fechamento)
    if (
        not fech
        or fech.sub_base != sub_base
        or fech.id_motoboy != motoboy_id
    ):
        raise HTTPException(404, "Fechamento não encontrado.")
    chave = _get_motoboy_chave_pix(db, motoboy_id)
    pdf = get_fechamento_pdf_bytes(db, fech, chave_pix=chave)
    codigo = build_fechamento_code(fech)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{codigo}.pdf"',
        },
    )
