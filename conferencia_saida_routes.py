"""Endpoints de Conferência de Saída (staff)."""
from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_current_user
from conferencia_saida_service import (
    STATUS_CONFERIDA,
    STATUS_PENDENTE,
    STATUS_RECONFERIR,
    carregar_nomes_motoboy,
    conferir_saida,
    contar_novos_por_motoboy_dia,
    listar_saidas_motoboy_dia,
    listar_saidas_novas_apos_conferencia,
    owner_conferencia_habilitada,
    resumo_novos_pacotes,
    somar_servicos_saidas,
)
from db import get_db
from models import ConferenciaSaida, User

router = APIRouter(prefix="/conferencias-saida", tags=["ConferenciasSaida"])

AbaTipo = Literal["pendente", "reconferir", "conferida"]


class ConferenciaItemOut(BaseModel):
    id: int
    motoboy_id: int
    motoboy_nome: str
    data_ref: date
    status: str
    qtd_no_momento: Optional[int] = None
    conferido_em: Optional[str] = None
    ultima_abertura_em: Optional[str] = None
    # Pacotes que entraram após a última conferência (só relevante em reconferir).
    novos_qtd: Optional[int] = None


class ConferenciaListOut(BaseModel):
    items: List[ConferenciaItemOut]
    total: int


class PacoteNovoOut(BaseModel):
    codigo: str
    servico: str


class ConferenciaDetalheOut(BaseModel):
    motoboy_id: int
    motoboy_nome: str
    data_ref: date
    status: str
    sum_shopee: int
    sum_mercado: int
    sum_avulso: int
    total: int
    qtd_no_momento: Optional[int] = None
    conferido_em: Optional[str] = None
    novos_qtd: int = 0
    novos_shopee: int = 0
    novos_mercado: int = 0
    novos_avulso: int = 0
    novos_pacotes: List[PacoteNovoOut] = Field(default_factory=list)


class ConferirBody(BaseModel):
    data_ref: date = Field(...)


def _require_staff(user: User) -> None:
    role = int(getattr(user, "role", 0) or 0)
    if role not in (0, 1, 2, 3):
        raise HTTPException(status_code=403, detail="Acesso restrito a admin/operador.")


def _require_flag(db: Session, user: User, sub_base: str) -> None:
    if not owner_conferencia_habilitada(db, sub_base, user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CONFERENCIA_DESABILITADA",
                "message": "Conferência de saída não está habilitada para esta base.",
            },
        )


def _detalhe_out(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    data_ref: date,
    row: ConferenciaSaida,
) -> ConferenciaDetalheOut:
    saidas = listar_saidas_motoboy_dia(
        db, sub_base=sub_base, motoboy_id=motoboy_id, data_ref=data_ref
    )
    shopee, ml, avulso = somar_servicos_saidas(saidas)
    nome = carregar_nomes_motoboy(db, [motoboy_id]).get(motoboy_id, f"Motoboy {motoboy_id}")

    novos_payload = {
        "novos_qtd": 0,
        "novos_shopee": 0,
        "novos_mercado": 0,
        "novos_avulso": 0,
        "novos_pacotes": [],
    }
    if row.status == STATUS_RECONFERIR:
        novos = listar_saidas_novas_apos_conferencia(
            db, sub_base=sub_base, motoboy_id=motoboy_id, data_ref=data_ref
        )
        novos_payload = resumo_novos_pacotes(novos)

    return ConferenciaDetalheOut(
        motoboy_id=motoboy_id,
        motoboy_nome=nome,
        data_ref=data_ref,
        status=row.status,
        sum_shopee=shopee,
        sum_mercado=ml,
        sum_avulso=avulso,
        total=shopee + ml + avulso,
        qtd_no_momento=row.qtd_no_momento,
        conferido_em=row.conferido_em.isoformat() if row.conferido_em else None,
        novos_qtd=int(novos_payload["novos_qtd"]),
        novos_shopee=int(novos_payload["novos_shopee"]),
        novos_mercado=int(novos_payload["novos_mercado"]),
        novos_avulso=int(novos_payload["novos_avulso"]),
        novos_pacotes=[PacoteNovoOut(**p) for p in novos_payload["novos_pacotes"]],
    )


@router.get("", response_model=ConferenciaListOut)
def listar_conferencias(
    data_inicio: date = Query(...),
    data_fim: date = Query(...),
    aba: AbaTipo = Query("pendente"),
    motoboy_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_staff(current_user)
    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(403, "Sub-base não definida.")
    _require_flag(db, current_user, sub_base)

    if data_inicio > data_fim:
        raise HTTPException(400, "data_inicio deve ser anterior a data_fim.")

    status_map = {
        "pendente": STATUS_PENDENTE,
        "reconferir": STATUS_RECONFERIR,
        "conferida": STATUS_CONFERIDA,
    }
    status = status_map[aba]

    q = select(ConferenciaSaida).where(
        ConferenciaSaida.sub_base == sub_base,
        ConferenciaSaida.data_ref >= data_inicio,
        ConferenciaSaida.data_ref <= data_fim,
        ConferenciaSaida.status == status,
    )
    if motoboy_id is not None:
        q = q.where(ConferenciaSaida.motoboy_id == motoboy_id)

    rows = list(db.scalars(q).all())
    nomes = carregar_nomes_motoboy(db, [int(r.motoboy_id) for r in rows])

    novos_map = {}
    if status == STATUS_RECONFERIR and rows:
        novos_map = contar_novos_por_motoboy_dia(
            db,
            sub_base=sub_base,
            chaves=[(int(r.motoboy_id), r.data_ref) for r in rows],
        )

    items = [
        ConferenciaItemOut(
            id=int(r.id),
            motoboy_id=int(r.motoboy_id),
            motoboy_nome=nomes.get(int(r.motoboy_id), f"Motoboy {r.motoboy_id}"),
            data_ref=r.data_ref,
            status=r.status,
            qtd_no_momento=r.qtd_no_momento,
            conferido_em=r.conferido_em.isoformat() if r.conferido_em else None,
            ultima_abertura_em=r.ultima_abertura_em.isoformat() if r.ultima_abertura_em else None,
            novos_qtd=(
                novos_map.get((int(r.motoboy_id), r.data_ref), 0)
                if status == STATUS_RECONFERIR
                else None
            ),
        )
        for r in rows
    ]
    items.sort(key=lambda x: (x.motoboy_nome or "").casefold())
    return ConferenciaListOut(items=items, total=len(items))


@router.get("/{motoboy_id}", response_model=ConferenciaDetalheOut)
def detalhe_conferencia(
    motoboy_id: int,
    data_ref: date = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_staff(current_user)
    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(403, "Sub-base não definida.")
    _require_flag(db, current_user, sub_base)

    row = db.scalar(
        select(ConferenciaSaida).where(
            ConferenciaSaida.sub_base == sub_base,
            ConferenciaSaida.motoboy_id == motoboy_id,
            ConferenciaSaida.data_ref == data_ref,
        )
    )
    if row is None:
        raise HTTPException(404, "Conferência não encontrada para este motoboy/dia.")

    return _detalhe_out(
        db, sub_base=sub_base, motoboy_id=motoboy_id, data_ref=data_ref, row=row
    )


@router.post("/{motoboy_id}/conferir", response_model=ConferenciaDetalheOut)
def post_conferir(
    motoboy_id: int,
    body: ConferirBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_staff(current_user)
    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(403, "Sub-base não definida.")
    _require_flag(db, current_user, sub_base)

    try:
        row = conferir_saida(
            db,
            sub_base=sub_base,
            motoboy_id=motoboy_id,
            data_ref=body.data_ref,
            user_id=getattr(current_user, "id", None),
        )
        db.commit()
        db.refresh(row)
    except ValueError:
        db.rollback()
        raise HTTPException(404, "Conferência não encontrada para este motoboy/dia.")
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Erro ao conferir saída: {e}")

    return _detalhe_out(
        db, sub_base=sub_base, motoboy_id=motoboy_id, data_ref=body.data_ref, row=row
    )
