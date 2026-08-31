"""
Fechamentos do motoboy autenticado (lista, detalhe, PDF).
Prefixo: /mobile/fechamentos
"""
from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_current_user
from db import get_db
from fechamento_pdf_service import (
    build_fechamento_code,
    get_fechamento_pdf_bytes,
    montar_resumo_e_datas,
)
from models import EntregadorFechamento, Motoboy, User
from entregador_fechamento_routes import _get_motoboy_chave_pix
from conferencia_saida_service import (
    conferencia_por_dia_periodo,
    owner_conferencia_habilitada,
)
from conferencia_saida_pure import montar_dias_conferencia_periodo

router = APIRouter(prefix="/mobile/fechamentos", tags=["Mobile Fechamentos"])

# Desligado por padrão: app mobile antigo ainda mostra "Baixar PDF", mas a API bloqueia.
# Para reativar: FECHAMENTO_PDF_MOBILE_ENABLED=true
def _pdf_mobile_enabled() -> bool:
    return os.getenv("FECHAMENTO_PDF_MOBILE_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
    )


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


def _faz_coleta(user: User, fech: EntregadorFechamento) -> bool:
    """Diária só para quem coleta: flag da sessão ou diária já apurada no período."""
    if bool(getattr(user, "pode_realizar_coleta", False)):
        return True
    if bool(getattr(user, "coletador", False)):
        return True
    return int(getattr(fech, "qtd_dias_coleta", 0) or 0) > 0


class FechamentoServicoResumoOut(BaseModel):
    feitos: int = 0
    cancelados: int = 0
    valor_feitos: Decimal = Decimal("0.00")
    valor_cancelados: Decimal = Decimal("0.00")


class FechamentoPorServicoOut(BaseModel):
    shopee: FechamentoServicoResumoOut = Field(default_factory=FechamentoServicoResumoOut)
    flex: FechamentoServicoResumoOut = Field(default_factory=FechamentoServicoResumoOut)
    avulso: FechamentoServicoResumoOut = Field(default_factory=FechamentoServicoResumoOut)


class FechamentoResumoOut(BaseModel):
    feitos: int = 0
    cancelados: int = 0
    pacotes_grandes: int = 0
    valor_bruto: Decimal = Decimal("0.00")
    valor_cancelados: Decimal = Decimal("0.00")
    ajustes: Decimal = Decimal("0.00")
    por_servico: FechamentoPorServicoOut = Field(default_factory=FechamentoPorServicoOut)


class FechamentoConferenciaDiaOut(BaseModel):
    data: str
    conferido: bool
    label: str


class FechamentoConferenciaOut(BaseModel):
    habilitada: bool = False
    dias: List[FechamentoConferenciaDiaOut] = Field(default_factory=list)


class FechamentoMobileOut(BaseModel):
    id_fechamento: int
    codigo: str
    periodo_inicio: date
    periodo_fim: date
    valor_base: Decimal
    valor_entregas: Decimal = Decimal("0.00")
    valor_coletas: Decimal = Decimal("0.00")
    qtd_dias_coleta: int = 0
    faz_coleta: bool = False
    valor_adicao: Decimal
    valor_subtracao: Decimal
    valor_final: Decimal
    motivo_adicao: Optional[str] = None
    motivo_subtracao: Optional[str] = None
    status: str
    chave_pix: Optional[str] = None
    criado_em: Optional[datetime] = None
    tem_pdf: bool = False
    resumo: Optional[FechamentoResumoOut] = None
    conferencia: Optional[FechamentoConferenciaOut] = None


def _to_out(
    fech: EntregadorFechamento,
    chave_pix: Optional[str],
    *,
    resumo: Optional[FechamentoResumoOut] = None,
    faz_coleta: bool = False,
    conferencia: Optional[FechamentoConferenciaOut] = None,
) -> FechamentoMobileOut:
    qtd_coleta = int(fech.qtd_dias_coleta or 0)
    return FechamentoMobileOut(
        id_fechamento=fech.id_fechamento,
        codigo=build_fechamento_code(fech),
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_base=fech.valor_base,
        valor_entregas=fech.valor_entregas,
        valor_coletas=fech.valor_coletas if faz_coleta else Decimal("0.00"),
        qtd_dias_coleta=qtd_coleta if faz_coleta else 0,
        faz_coleta=faz_coleta,
        valor_adicao=fech.valor_adicao,
        valor_subtracao=fech.valor_subtracao,
        valor_final=fech.valor_final,
        motivo_adicao=fech.motivo_adicao,
        motivo_subtracao=fech.motivo_subtracao,
        status=fech.status,
        chave_pix=chave_pix,
        criado_em=fech.criado_em,
        tem_pdf=bool((fech.pdf_object_key or "").strip()),
        resumo=resumo,
        conferencia=conferencia,
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
    return [_to_out(r, chave, faz_coleta=_faz_coleta(current_user, r)) for r in rows]


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
    resumo_dict, datas = montar_resumo_e_datas(db, fech)
    resumo = FechamentoResumoOut.model_validate(resumo_dict)
    conferencia = FechamentoConferenciaOut(habilitada=False)
    if owner_conferencia_habilitada(db, sub_base, current_user):
        registros = conferencia_por_dia_periodo(
            db,
            sub_base=sub_base,
            motoboy_id=motoboy_id,
            periodo_inicio=fech.periodo_inicio,
            periodo_fim=fech.periodo_fim,
        )
        dias = montar_dias_conferencia_periodo(registros, datas)
        conferencia = FechamentoConferenciaOut(
            habilitada=True,
            dias=[FechamentoConferenciaDiaOut.model_validate(d) for d in dias],
        )
    return _to_out(
        fech,
        chave,
        resumo=resumo,
        faz_coleta=_faz_coleta(current_user, fech),
        conferencia=conferencia,
    )


@router.get("/{id_fechamento}/pdf")
def baixar_pdf_fechamento(
    id_fechamento: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not _pdf_mobile_enabled():
        raise HTTPException(403, "Download de PDF temporariamente indisponível.")
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
