"""
Rotas de Fechamento de Bases (Coletas)
GET /coletas/fechamentos/calcular — preview
POST /coletas/fechamentos — criar
GET /coletas/fechamentos/{id_fechamento} — obter (para modal)
PATCH /coletas/fechamentos/{id_fechamento} — reajustar
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import BaseFechamento, BaseFechamentoItem, Coleta, Saida, BasePreco
from models import User

from coletas import _sub_base_from_token_or_422, _get_precos_cached, _decimal

router = APIRouter(prefix="/fechamentos", tags=["Fechamentos Bases"])

STATUS_GERADO = "GERADO"
STATUS_REAJUSTADO = "REAJUSTADO"


def _normalizar_servico_saida(serv: str) -> str:
    """Mapeia Saida.servico para shopee | ml | avulso."""
    s = (serv or "").lower().strip()
    if "shopee" in s:
        return "shopee"
    if "mercado" in s or "ml" in s or "flex" in s:
        return "ml"
    return "avulso"


def _build_itens_e_valores(
    db: Session,
    sub_base: str,
    base: str,
    periodo_inicio: date,
    periodo_fim: date,
) -> tuple[List[dict], Decimal, Decimal, Decimal]:
    """
    Retorna (itens, valor_bruto, valor_cancelados, valor_final).
    itens: lista de {data, shopee, mercado_livre, avulso, cancelados_shopee, cancelados_ml, cancelados_avulso}
    """
    base_norm = base.strip()
    base_key = base_norm.upper()
    from datetime import time as dt_time
    dt_start = datetime.combine(periodo_inicio, dt_time.min)
    dt_end = datetime.combine(periodo_fim, dt_time(23, 59, 59))

    # Coletas: agrupar por (data, base)
    stmt_coletas = select(Coleta).where(
        Coleta.sub_base == sub_base,
        func.upper(Coleta.base) == base_key,
        Coleta.timestamp >= dt_start,
        Coleta.timestamp <= dt_end,
    ).where(
        (Coleta.shopee > 0) | (Coleta.mercado_livre > 0) | (Coleta.avulso > 0) | (Coleta.valor_total > 0)
    )
    rows_coletas = db.scalars(stmt_coletas).all()

    # Cancelados: Saidas com status cancelado, agrupar por (data, base, servico)
    stmt_canc = select(Saida).where(
        Saida.sub_base == sub_base,
        func.lower(Saida.status) == "cancelado",
    )
    if base_norm:
        stmt_canc = stmt_canc.where(func.upper(Saida.base) == base_key)
    stmt_canc = stmt_canc.where(
        Saida.data >= periodo_inicio,
        Saida.data <= periodo_fim,
    )
    rows_canc = db.scalars(stmt_canc).all()

    # Mapa coletas por data (agrupar manuais e codigo)
    mapa_coletas: dict[str, dict] = {}
    for r in rows_coletas:
        dia = r.timestamp.date().isoformat()
        if dia not in mapa_coletas:
            mapa_coletas[dia] = {"shopee": 0, "mercado_livre": 0, "avulso": 0}
        mapa_coletas[dia]["shopee"] += r.shopee or 0
        mapa_coletas[dia]["mercado_livre"] += r.mercado_livre or 0
        mapa_coletas[dia]["avulso"] += r.avulso or 0

    # Mapa cancelados por data e tipo
    mapa_canc: dict[str, dict] = {}
    for c in rows_canc:
        dia = (c.data or c.timestamp.date()).isoformat()
        tipo = _normalizar_servico_saida(c.servico or "")
        if dia not in mapa_canc:
            mapa_canc[dia] = {"shopee": 0, "ml": 0, "avulso": 0}
        mapa_canc[dia][tipo] = mapa_canc[dia].get(tipo, 0) + 1

    # Dias únicos (coletas + cancelados)
    dias_set = set(mapa_coletas.keys()) | set(mapa_canc.keys())
    if not dias_set:
        return [], Decimal("0.00"), Decimal("0.00"), Decimal("0.00")

    try:
        p_shopee, p_ml, p_avulso = _get_precos_cached(db, sub_base, base_norm)
    except HTTPException:
        p_shopee = p_ml = p_avulso = Decimal("0.00")

    itens = []
    valor_bruto = Decimal("0.00")
    valor_cancelados = Decimal("0.00")

    for dia in sorted(dias_set):
        c = mapa_coletas.get(dia, {})
        canc = mapa_canc.get(dia, {})
        shopee = c.get("shopee", 0)
        ml = c.get("mercado_livre", 0)
        avulso = c.get("avulso", 0)
        canc_s = canc.get("shopee", 0)
        canc_ml = canc.get("ml", 0)
        canc_a = canc.get("avulso", 0)

        v_bruto = (_decimal(shopee) * p_shopee + _decimal(ml) * p_ml + _decimal(avulso) * p_avulso).quantize(Decimal("0.01"))
        v_canc = (_decimal(canc_s) * p_shopee + _decimal(canc_ml) * p_ml + _decimal(canc_a) * p_avulso).quantize(Decimal("0.01"))

        valor_bruto += v_bruto
        valor_cancelados += v_canc

        itens.append({
            "data": dia,
            "shopee": shopee,
            "mercado_livre": ml,
            "avulso": avulso,
            "cancelados_shopee": canc_s,
            "cancelados_ml": canc_ml,
            "cancelados_avulso": canc_a,
        })

    valor_final = (valor_bruto - valor_cancelados).quantize(Decimal("0.01"))
    return itens, valor_bruto, valor_cancelados, valor_final


# =========================================================
# SCHEMAS
# =========================================================

class FechamentoItemIn(BaseModel):
    data: str
    shopee: int = 0
    mercado_livre: int = 0
    avulso: int = 0
    cancelados_shopee: int = 0
    cancelados_ml: int = 0
    cancelados_avulso: int = 0


class FechamentoItemOut(BaseModel):
    data: str
    shopee: int
    mercado_livre: int
    avulso: int
    cancelados_shopee: int
    cancelados_ml: int
    cancelados_avulso: int


class FechamentoCreate(BaseModel):
    base: str = Field(min_length=1)
    periodo_inicio: date
    periodo_fim: date
    itens: Optional[List[FechamentoItemIn]] = None


class FechamentoUpdate(BaseModel):
    itens: List[FechamentoItemIn]


class CalcularOut(BaseModel):
    base: str
    periodo_inicio: str
    periodo_fim: str
    valor_bruto: Decimal
    valor_cancelados: Decimal
    valor_final: Decimal
    itens: List[FechamentoItemOut]
    precos: dict


class FechamentoOut(BaseModel):
    id_fechamento: int
    sub_base: str
    base: str
    periodo_inicio: date
    periodo_fim: date
    valor_bruto: Decimal
    valor_cancelados: Decimal
    valor_final: Decimal
    status: str
    criado_em: Optional[datetime] = None
    itens: List[FechamentoItemOut]


# =========================================================
# GET — Calcular (preview)
# =========================================================

@router.get("/calcular", response_model=CalcularOut)
def calcular_fechamento(
    base: str = Query(..., min_length=1),
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base_from_token_or_422(current_user)
    if periodo_inicio > periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")

    itens, valor_bruto, valor_cancelados, valor_final = _build_itens_e_valores(
        db, sub_base, base, periodo_inicio, periodo_fim
    )
    try:
        p_s, p_m, p_a = _get_precos_cached(db, sub_base, base.strip())
        precos = {"shopee": float(p_s), "ml": float(p_m), "avulso": float(p_a)}
    except HTTPException:
        precos = {"shopee": 0, "ml": 0, "avulso": 0}

    return CalcularOut(
        base=base.strip(),
        periodo_inicio=periodo_inicio.isoformat(),
        periodo_fim=periodo_fim.isoformat(),
        valor_bruto=valor_bruto,
        valor_cancelados=valor_cancelados,
        valor_final=valor_final,
        itens=[FechamentoItemOut(**x) for x in itens],
        precos=precos,
    )


# =========================================================
# POST — Criar fechamento
# =========================================================

@router.post("", response_model=FechamentoOut, status_code=201)
def criar_fechamento(
    payload: FechamentoCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base_from_token_or_422(current_user)
    if payload.periodo_inicio > payload.periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")

    base_norm = payload.base.strip()

    # Verificar duplicidade
    existente = db.scalar(
        select(BaseFechamento).where(
            BaseFechamento.sub_base == sub_base,
            BaseFechamento.base == base_norm,
            BaseFechamento.periodo_inicio == payload.periodo_inicio,
            BaseFechamento.periodo_fim == payload.periodo_fim,
        )
    )
    if existente:
        raise HTTPException(409, "Já existe fechamento para esta base e período.")

    if payload.itens:
        itens_data = payload.itens
        valor_bruto = Decimal("0.00")
        valor_cancelados = Decimal("0.00")
        try:
            p_s, p_m, p_a = _get_precos_cached(db, sub_base, base_norm)
        except HTTPException:
            p_s = p_m = p_a = Decimal("0.00")
        for it in itens_data:
            v_bruto = _decimal(it.shopee) * p_s + _decimal(it.mercado_livre) * p_m + _decimal(it.avulso) * p_a
            v_canc = _decimal(it.cancelados_shopee) * p_s + _decimal(it.cancelados_ml) * p_m + _decimal(it.cancelados_avulso) * p_a
            valor_bruto += v_bruto
            valor_cancelados += v_canc
        valor_final = (valor_bruto - valor_cancelados).quantize(Decimal("0.01"))
    else:
        itens_data, valor_bruto, valor_cancelados, valor_final = _build_itens_e_valores(
            db, sub_base, base_norm, payload.periodo_inicio, payload.periodo_fim
        )
        itens_data = [FechamentoItemIn(**x) for x in itens_data]

    fech = BaseFechamento(
        sub_base=sub_base,
        base=base_norm,
        periodo_inicio=payload.periodo_inicio,
        periodo_fim=payload.periodo_fim,
        valor_bruto=valor_bruto,
        valor_cancelados=valor_cancelados,
        valor_final=valor_final,
        status=STATUS_GERADO,
    )
    db.add(fech)
    db.flush()

    for it in itens_data:
        dt_val = datetime.strptime(it.data, "%Y-%m-%d").date() if isinstance(it.data, str) else it.data
        item = BaseFechamentoItem(
            id_fechamento=fech.id_fechamento,
            data=dt_val,
            shopee=it.shopee,
            mercado_livre=it.mercado_livre,
            avulso=it.avulso,
            cancelados_shopee=it.cancelados_shopee,
            cancelados_ml=it.cancelados_ml,
            cancelados_avulso=it.cancelados_avulso,
        )
        db.add(item)

    db.commit()
    db.refresh(fech)

    itens_out = [FechamentoItemOut(
        data=it.data if isinstance(it.data, str) else it.data.isoformat(),
        shopee=it.shopee, mercado_livre=it.mercado_livre, avulso=it.avulso,
        cancelados_shopee=it.cancelados_shopee, cancelados_ml=it.cancelados_ml, cancelados_avulso=it.cancelados_avulso,
    ) for it in itens_data]

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        base=fech.base,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_bruto=fech.valor_bruto,
        valor_cancelados=fech.valor_cancelados,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        itens=itens_out,
    )


# =========================================================
# GET — Obter fechamento (para modal)
# =========================================================

@router.get("/{id_fechamento}", response_model=FechamentoOut)
def obter_fechamento(
    id_fechamento: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base_from_token_or_422(current_user)
    fech = db.get(BaseFechamento, id_fechamento)
    if not fech or fech.sub_base != sub_base:
        raise HTTPException(404, "Fechamento não encontrado.")

    itens = db.scalars(
        select(BaseFechamentoItem)
        .where(BaseFechamentoItem.id_fechamento == id_fechamento)
        .order_by(BaseFechamentoItem.data)
    ).all()

    itens_out = [
        FechamentoItemOut(
            data=i.data.isoformat(),
            shopee=i.shopee,
            mercado_livre=i.mercado_livre,
            avulso=i.avulso,
            cancelados_shopee=i.cancelados_shopee,
            cancelados_ml=i.cancelados_ml,
            cancelados_avulso=i.cancelados_avulso,
        )
        for i in itens
    ]

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        base=fech.base,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_bruto=fech.valor_bruto,
        valor_cancelados=fech.valor_cancelados,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        itens=itens_out,
    )


# =========================================================
# PATCH — Reajustar fechamento
# =========================================================

@router.patch("/{id_fechamento}", response_model=FechamentoOut)
def atualizar_fechamento(
    id_fechamento: int,
    payload: FechamentoUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base_from_token_or_422(current_user)
    fech = db.get(BaseFechamento, id_fechamento)
    if not fech or fech.sub_base != sub_base:
        raise HTTPException(404, "Fechamento não encontrado.")
    if (fech.status or "").upper() != STATUS_GERADO:
        raise HTTPException(400, "Apenas fechamentos com status GERADO podem ser reajustados.")

    try:
        p_s, p_m, p_a = _get_precos_cached(db, sub_base, fech.base)
    except HTTPException:
        p_s = p_m = p_a = Decimal("0.00")

    valor_bruto = Decimal("0.00")
    valor_cancelados = Decimal("0.00")
    for it in payload.itens:
        v_bruto = _decimal(it.shopee) * p_s + _decimal(it.mercado_livre) * p_m + _decimal(it.avulso) * p_a
        v_canc = _decimal(it.cancelados_shopee) * p_s + _decimal(it.cancelados_ml) * p_m + _decimal(it.cancelados_avulso) * p_a
        valor_bruto += v_bruto
        valor_cancelados += v_canc
    valor_final = (valor_bruto - valor_cancelados).quantize(Decimal("0.01"))

    fech.valor_bruto = valor_bruto
    fech.valor_cancelados = valor_cancelados
    fech.valor_final = valor_final
    fech.status = STATUS_REAJUSTADO

    # Remover itens antigos e inserir novos
    for i in db.scalars(select(BaseFechamentoItem).where(BaseFechamentoItem.id_fechamento == id_fechamento)).all():
        db.delete(i)
    db.flush()

    for it in payload.itens:
        dt_val = datetime.strptime(it.data, "%Y-%m-%d").date() if isinstance(it.data, str) else it.data
        item = BaseFechamentoItem(
            id_fechamento=id_fechamento,
            data=dt_val,
            shopee=it.shopee,
            mercado_livre=it.mercado_livre,
            avulso=it.avulso,
            cancelados_shopee=it.cancelados_shopee,
            cancelados_ml=it.cancelados_ml,
            cancelados_avulso=it.cancelados_avulso,
        )
        db.add(item)

    db.commit()
    db.refresh(fech)

    itens = db.scalars(
        select(BaseFechamentoItem).where(BaseFechamentoItem.id_fechamento == id_fechamento).order_by(BaseFechamentoItem.data)
    ).all()
    itens_out = [
        FechamentoItemOut(
            data=i.data.isoformat(),
            shopee=i.shopee, mercado_livre=i.mercado_livre, avulso=i.avulso,
            cancelados_shopee=i.cancelados_shopee, cancelados_ml=i.cancelados_ml, cancelados_avulso=i.cancelados_avulso,
        )
        for i in itens
    ]

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        base=fech.base,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_bruto=fech.valor_bruto,
        valor_cancelados=fech.valor_cancelados,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        itens=itens_out,
    )
