"""
Rotas de Acompanhamento do Dia (performance por motoboy por data).
Prefixo: /acompanhamento
"""
from __future__ import annotations

from datetime import date
from typing import Optional, List, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, Saida, SaidaDetail, SaidaHistorico, Motoboy, RotasMotoboy
from saidas_routes import (
    normalizar_status_saida,
    STATUS_SAIU_PARA_ENTREGA,
    STATUS_EM_ROTA,
    STATUS_ENTREGUE,
    STATUS_AUSENTE,
)
from saida_operacional_utils import carregar_contexto_operacional

router = APIRouter(prefix="/acompanhamento", tags=["Acompanhamento"])

_MAX_DIAS_PERIODO = 31


def _resolver_periodo(
    data: Optional[date],
    data_inicio: Optional[date],
    data_fim: Optional[date],
) -> Tuple[date, date]:
    """
    Resolve período inclusivo.
    - data_inicio + data_fim → intervalo
    - só data → dia único
    - nada → hoje
    """
    if data_inicio is not None or data_fim is not None:
        if data_inicio is None or data_fim is None:
            raise HTTPException(
                status_code=400,
                detail="Informe data_inicio e data_fim juntos.",
            )
        if data_inicio > data_fim:
            raise HTTPException(
                status_code=400,
                detail="data_inicio não pode ser maior que data_fim.",
            )
        if (data_fim - data_inicio).days > _MAX_DIAS_PERIODO:
            raise HTTPException(
                status_code=400,
                detail=f"Período máximo de {_MAX_DIAS_PERIODO} dias.",
            )
        return data_inicio, data_fim
    ref = data or date.today()
    return ref, ref


def _carregar_nomes_motoboy_ids(db: Session, motoboy_ids: List[int]) -> dict[int, str]:
    ids = sorted({int(mid) for mid in motoboy_ids if mid is not None})
    if not ids:
        return {}
    rows_motoboy = db.execute(
        select(Motoboy.id_motoboy, Motoboy.user_id).where(Motoboy.id_motoboy.in_(ids))
    ).all()
    motoboy_user_map = {
        int(mid): (int(uid) if uid is not None else None)
        for mid, uid in rows_motoboy
    }
    user_ids = sorted({uid for uid in motoboy_user_map.values() if uid is not None})
    user_map = {}
    if user_ids:
        rows_user = db.execute(
            select(User.id, User.nome, User.sobrenome, User.username).where(User.id.in_(user_ids))
        ).all()
        user_map = {
            int(uid): ((nome or ""), (sobrenome or ""), (username or ""))
            for uid, nome, sobrenome, username in rows_user
        }
    out: dict[int, str] = {}
    for mid in ids:
        uid = motoboy_user_map.get(mid)
        if uid is None:
            out[mid] = f"Motoboy {mid}"
            continue
        nome, sobrenome, username = user_map.get(uid, ("", "", ""))
        out[mid] = (f"{nome} {sobrenome}".strip() or username or f"Motoboy {mid}")
    return out


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
    data_inicio: Optional[str] = None
    data_fim: Optional[str] = None


class AcompanhamentoSaidasDiaResponse(BaseModel):
    data: str
    motoboy_id: int
    motoboy_nome: str
    pendentes_hoje: int
    sum_shopee: int
    sum_mercado: int
    sum_avulso: int
    data_inicio: Optional[str] = None
    data_fim: Optional[str] = None


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
    nomes_motoboy = _carregar_nomes_motoboy_ids(
        db,
        [int(saida.motoboy_id) for saida, _ in rows if getattr(saida, "motoboy_id", None) is not None],
    )
    items = []
    for saida, detail in rows:
        motoboy_nome = nomes_motoboy.get(int(saida.motoboy_id)) if saida.motoboy_id else None
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
    data: Optional[date] = Query(None, description="Data única (YYYY-MM-DD)"),
    data_inicio: Optional[date] = Query(None, description="Início do período (YYYY-MM-DD)"),
    data_fim: Optional[date] = Query(None, description="Fim do período (YYYY-MM-DD)"),
    motoboy_id: Optional[int] = Query(None, description="Filtrar por motoboy"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Retorna desempenho por motoboy na data ou período informado.
    Agrupa Saida por motoboy_id; inclui totais e SLA.
    """
    sub_base = getattr(current_user, "sub_base", None)
    if not sub_base or not str(sub_base).strip():
        raise HTTPException(status_code=403, detail="Sub_base não definida.")

    inicio, fim = _resolver_periodo(data, data_inicio, data_fim)
    label_data = fim.isoformat() if inicio == fim else f"{inicio.isoformat()}_{fim.isoformat()}"

    stmt = (
        select(Saida)
        .where(Saida.sub_base == sub_base)
        .where(Saida.data >= inicio)
        .where(Saida.data <= fim)
        .where(Saida.motoboy_id.isnot(None))
    )
    if motoboy_id is not None:
        stmt = stmt.where(Saida.motoboy_id == motoboy_id)

    saidas = list(db.scalars(stmt).all())

    # Cache: id_saida -> max(timestamp) do evento "entregue" em saida_historico (horário correto)
    ids_saida = [s.id_saida for s in saidas]
    cache_entregue: dict[int, object] = {}
    if ids_saida:
        stmt_entregue = (
            select(SaidaHistorico.id_saida, func.max(SaidaHistorico.timestamp).label("ts"))
            .where(SaidaHistorico.id_saida.in_(ids_saida))
            .where(SaidaHistorico.evento == "entregue")
            .group_by(SaidaHistorico.id_saida)
        )
        for row in db.execute(stmt_entregue).all():
            cache_entregue[row.id_saida] = row.ts

    # Agrupar por motoboy_id
    by_motoboy: dict[int, list] = {}
    for s in saidas:
        mid = s.motoboy_id
        if mid not in by_motoboy:
            by_motoboy[mid] = []
        by_motoboy[mid].append(s)

    # Rotas no período por motoboy (ativa ou finalizada)
    rotas_map = {}  # motoboy_id -> "Rota" | "SEM ROTA"
    if by_motoboy:
        rota_stmt = (
            select(RotasMotoboy.motoboy_id, RotasMotoboy.status)
            .where(RotasMotoboy.data >= inicio)
            .where(RotasMotoboy.data <= fim)
            .where(RotasMotoboy.motoboy_id.in_(list(by_motoboy.keys())))
        )
        for row in db.execute(rota_stmt).all():
            mid, status = row[0], (row[1] or "").strip().lower()
            current = rotas_map.get(mid)
            if status == "ativa":
                rotas_map[mid] = "Ativa"
            elif current != "Ativa":
                rotas_map[mid] = "Rota"
    for mid in by_motoboy:
        if mid not in rotas_map:
            rotas_map[mid] = "SEM ROTA"

    items = []
    totais_pedidos = 0
    totais_entregues = 0
    totais_em_rota = 0
    totais_ausente = 0

    nomes_motoboy = _carregar_nomes_motoboy_ids(db, [int(mid) for mid in by_motoboy.keys()])
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
            ts_entrega = cache_entregue.get(s.id_saida) if s.id_saida in cache_entregue else s.data_hora_entrega
            if ts_entrega:
                if ultima_entrega_dt is None or ts_entrega > ultima_entrega_dt:
                    ultima_entrega_dt = ts_entrega

        sla = round(100.0 * entregues / pedidos, 1) if pedidos > 0 else None

        motoboy_nome = nomes_motoboy.get(int(mid), f"Motoboy {mid}")

        items.append(
            AcompanhamentoItem(
                data=label_data,
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
        data_inicio=inicio.isoformat(),
        data_fim=fim.isoformat(),
    )


@router.get("/saidas-dia", response_model=AcompanhamentoSaidasDiaResponse)
def acompanhamento_saidas_dia(
    motoboy_id: int = Query(..., description="Motoboy obrigatório"),
    data: Optional[date] = Query(None, description="Data de referência (YYYY-MM-DD)"),
    data_inicio: Optional[date] = Query(None, description="Início do período (YYYY-MM-DD)"),
    data_fim: Optional[date] = Query(None, description="Fim do período (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Retorna os pendentes operacionais do motoboy na data/período, com a mesma
    regra de período operacional das telas de Registros e Mobile.
    """
    sub_base = getattr(current_user, "sub_base", None)
    if not sub_base or not str(sub_base).strip():
        raise HTTPException(status_code=403, detail="Sub_base não definida.")

    inicio, fim = _resolver_periodo(data, data_inicio, data_fim)
    rows_pendentes_all = db.scalars(
        select(Saida).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.codigo.isnot(None),
            Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]),
        )
    ).all()

    ctx_map = carregar_contexto_operacional(db, [s.id_saida for s in rows_pendentes_all])
    rows_pendentes_validos = [
        s
        for s in rows_pendentes_all
        if not (
            ctx_map.get(s.id_saida)
            and (
                ctx_map[s.id_saida].removido_sem_inicio_ativo
                or not ctx_map[s.id_saida].leitura_valida
            )
        )
    ]
    rows_pendentes_periodo = [
        s
        for s in rows_pendentes_validos
        if inicio
        <= (
            ((ctx_map.get(s.id_saida).operacional_ts if ctx_map.get(s.id_saida) else None) or s.timestamp).date()
        )
        <= fim
    ]

    sum_shopee = 0
    sum_mercado = 0
    sum_avulso = 0
    for s in rows_pendentes_periodo:
        srv = (s.servico or "").strip().lower()
        if ("shopee" in srv) or ("spx" in srv):
            sum_shopee += 1
        elif (
            ("mercado livre" in srv)
            or ("mercado_livre" in srv)
            or ("mercadolivre" in srv)
            or (" ml" in f" {srv}")
            or ("flex" in srv)
        ):
            sum_mercado += 1
        else:
            sum_avulso += 1

    motoboy_nome = _carregar_nomes_motoboy_ids(db, [motoboy_id]).get(
        motoboy_id, f"Motoboy {motoboy_id}"
    )

    return AcompanhamentoSaidasDiaResponse(
        data=fim.isoformat(),
        motoboy_id=motoboy_id,
        motoboy_nome=motoboy_nome,
        pendentes_hoje=len(rows_pendentes_periodo),
        sum_shopee=sum_shopee,
        sum_mercado=sum_mercado,
        sum_avulso=sum_avulso,
        data_inicio=inicio.isoformat(),
        data_fim=fim.isoformat(),
    )
