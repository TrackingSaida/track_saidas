"""
Rotas de Acompanhamento do Dia (performance por motoboy por data).
Prefixo: /acompanhamento
"""
from __future__ import annotations

from datetime import date
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, Saida, SaidaDetail, Motoboy, RotasMotoboy
from saidas_routes import (
    _get_motoboy_nome,
    normalizar_status_saida,
    STATUS_SAIU_PARA_ENTREGA,
    STATUS_EM_ROTA,
    STATUS_ENTREGUE,
    STATUS_AUSENTE,
)

router = APIRouter(prefix="/acompanhamento", tags=["Acompanhamento"])


class AcompanhamentoItem(BaseModel):
    data: str
    motoboy_id: int
    motoboy_nome: str
    pedidos: int
    entregues: int
    em_rota: int
    ausente_ou_ocorrencias: int
    rota: str
    distancia_tempo: Optional[str] = None
    ultima_entrega: Optional[str] = None
    sla: Optional[float] = None


class AcompanhamentoTotais(BaseModel):
    pedidos: int
    entregues: int
    em_rota: int
    ausente_ou_ocorrencias: int
    sla: Optional[float] = None


class AcompanhamentoDiaResponse(BaseModel):
    items: List[AcompanhamentoItem]
    totais: AcompanhamentoTotais


class AcompanhamentoMapaItem(BaseModel):
    id_saida: int
    latitude: float
    longitude: float
    status: Optional[str] = None
    motoboy_id: Optional[int] = None
    motoboy_nome: Optional[str] = None
    codigo: Optional[str] = None
    endereco_formatado: Optional[str] = None


class AcompanhamentoMapaResponse(BaseModel):
    items: List[AcompanhamentoMapaItem]


@router.get("/mapa", response_model=AcompanhamentoMapaResponse)
def acompanhamento_mapa(
    data: date = Query(..., description="Data única (YYYY-MM-DD)"),
    motoboy_id: Optional[int] = Query(None, description="Filtrar por motoboy"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Retorna saídas do dia com coordenadas (lat/long) para exibição no mapa.
    Apenas registros com latitude e longitude preenchidos (SaidaDetail).
    """
    sub_base = getattr(current_user, "sub_base", None)
    if not sub_base or not str(sub_base).strip():
        raise HTTPException(status_code=403, detail="Sub_base não definida.")

    stmt = (
        select(Saida, SaidaDetail)
        .join(SaidaDetail, Saida.id_saida == SaidaDetail.id_saida)
        .where(Saida.sub_base == sub_base)
        .where(Saida.data == data)
        .where(SaidaDetail.latitude.isnot(None))
        .where(SaidaDetail.longitude.isnot(None))
    )
    if motoboy_id is not None:
        stmt = stmt.where(Saida.motoboy_id == motoboy_id)

    rows = db.execute(stmt).all()
    items = []
    for saida, detail in rows:
        motoboy = db.get(Motoboy, saida.motoboy_id) if saida.motoboy_id else None
        motoboy_nome = _get_motoboy_nome(db, motoboy) if motoboy else None
        lat = float(detail.latitude) if detail.latitude is not None else None
        lon = float(detail.longitude) if detail.longitude is not None else None
        if lat is None or lon is None:
            continue
        items.append(
            AcompanhamentoMapaItem(
                id_saida=saida.id_saida,
                latitude=lat,
                longitude=lon,
                status=saida.status,
                motoboy_id=saida.motoboy_id,
                motoboy_nome=motoboy_nome,
                codigo=saida.codigo,
                endereco_formatado=detail.endereco_formatado,
            )
        )
    return AcompanhamentoMapaResponse(items=items)


@router.get("/dia", response_model=AcompanhamentoDiaResponse)
def acompanhamento_dia(
    data: date = Query(..., description="Data única (YYYY-MM-DD)"),
    motoboy_id: Optional[int] = Query(None, description="Filtrar por motoboy"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Retorna desempenho por motoboy na data informada.
    Agrupa Saida por motoboy_id; inclui totais e SLA.
    """
    sub_base = getattr(current_user, "sub_base", None)
    if not sub_base or not str(sub_base).strip():
        raise HTTPException(status_code=403, detail="Sub_base não definida.")

    stmt = (
        select(Saida)
        .where(Saida.sub_base == sub_base)
        .where(Saida.data == data)
        .where(Saida.motoboy_id.isnot(None))
    )
    if motoboy_id is not None:
        stmt = stmt.where(Saida.motoboy_id == motoboy_id)

    saidas = list(db.scalars(stmt).all())

    # Agrupar por motoboy_id
    by_motoboy: dict[int, list] = {}
    for s in saidas:
        mid = s.motoboy_id
        if mid not in by_motoboy:
            by_motoboy[mid] = []
        by_motoboy[mid].append(s)

    # Rotas do dia por motoboy (ativa ou finalizada)
    rotas_map = {}  # motoboy_id -> "Rota" | "SEM ROTA"
    if by_motoboy:
        rota_stmt = (
            select(RotasMotoboy.motoboy_id, RotasMotoboy.status)
            .where(RotasMotoboy.data == data)
            .where(RotasMotoboy.motoboy_id.in_(list(by_motoboy.keys())))
        )
        for row in db.execute(rota_stmt).all():
            mid, status = row[0], (row[1] or "").strip().lower()
            rotas_map[mid] = "Ativa" if status == "ativa" else "Rota"
    for mid in by_motoboy:
        if mid not in rotas_map:
            rotas_map[mid] = "SEM ROTA"

    items = []
    totais_pedidos = 0
    totais_entregues = 0
    totais_em_rota = 0
    totais_ausente = 0

    for mid, list_saidas in sorted(by_motoboy.items()):
        pedidos = len(list_saidas)
        entregues = 0
        em_rota = 0
        ausente_ou_ocorrencias = 0
        ultima_entrega_dt = None

        for s in list_saidas:
            st = normalizar_status_saida(s.status)
            if st == STATUS_ENTREGUE:
                entregues += 1
            elif st == STATUS_EM_ROTA:
                em_rota += 1
            elif st == STATUS_AUSENTE:
                ausente_ou_ocorrencias += 1
            if s.data_hora_entrega:
                if ultima_entrega_dt is None or s.data_hora_entrega > ultima_entrega_dt:
                    ultima_entrega_dt = s.data_hora_entrega

        sla = round(100.0 * entregues / pedidos, 1) if pedidos > 0 else None

        motoboy = db.get(Motoboy, mid)
        motoboy_nome = _get_motoboy_nome(db, motoboy) if motoboy else f"Motoboy {mid}"

        items.append(
            AcompanhamentoItem(
                data=data.isoformat(),
                motoboy_id=mid,
                motoboy_nome=motoboy_nome,
                pedidos=pedidos,
                entregues=entregues,
                em_rota=em_rota,
                ausente_ou_ocorrencias=ausente_ou_ocorrencias,
                rota=rotas_map.get(mid, "SEM ROTA"),
                distancia_tempo=None,
                ultima_entrega=ultima_entrega_dt.isoformat() if ultima_entrega_dt else None,
                sla=sla,
            )
        )
        totais_pedidos += pedidos
        totais_entregues += entregues
        totais_em_rota += em_rota
        totais_ausente += ausente_ou_ocorrencias

    sla_total = round(100.0 * totais_entregues / totais_pedidos, 1) if totais_pedidos > 0 else None

    return AcompanhamentoDiaResponse(
        items=items,
        totais=AcompanhamentoTotais(
            pedidos=totais_pedidos,
            entregues=totais_entregues,
            em_rota=totais_em_rota,
            ausente_ou_ocorrencias=totais_ausente,
            sla=sla_total,
        ),
    )
