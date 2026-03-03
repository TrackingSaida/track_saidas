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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import (
    BaseFechamento,
    BaseFechamentoItem,
    BasePreco,
    BaseSellerDados,
    Coleta,
    Owner,
    Saida,
    User,
)

from coletas import _decimal, _get_precos_cached, _sub_base_from_token_or_422

router = APIRouter(prefix="/fechamentos", tags=["Fechamentos Bases"])

STATUS_GERADO = "GERADO"
STATUS_REAJUSTADO = "REAJUSTADO"


def _montar_seller_info(db: Session, sub_base: str, base: str) -> Optional[Dict[str, Any]]:
    """
    Retorna informações básicas do seller/base (nome, CNPJ e endereço completo)
    quando existirem em BaseSellerDados. Nunca lança erro; em caso de falta de
    dados retorna None.
    """
    base_norm = (base or "").strip()
    if not base_norm:
        return None

    # 1) Localiza cadastro de preços da base na sub_base
    preco = db.scalar(
        select(BasePreco).where(
            BasePreco.sub_base == sub_base,
            BasePreco.base == base_norm,
        )
    )
    seller: Optional[BaseSellerDados] = None

    # 2) Tenta primeiro pelo vínculo explícito base_id -> BaseSellerDados
    if preco and getattr(preco, "id_base", None) is not None:
        seller = db.scalar(
            select(BaseSellerDados).where(BaseSellerDados.base_id == preco.id_base)
        )

    # 3) Fallback: tenta resolver pelo Owner (username/sub_base) -> BaseSellerDados.owner_id
    if not seller and preco and getattr(preco, "username", None):
        owner = db.scalar(
            select(Owner).where(
                Owner.username == preco.username,
                Owner.sub_base == sub_base,
            )
        )
        if owner:
            seller = db.scalar(
                select(BaseSellerDados).where(BaseSellerDados.owner_id == owner.id_owner)
            )

    if not seller:
        return None

    # Monta endereço completo de forma resiliente a campos vazios
    partes_endereco: List[str] = []
    if seller.rua:
        rua_num = f"{seller.rua}".strip()
        if seller.numero:
            rua_num = f"{rua_num}, {seller.numero}".strip()
        partes_endereco.append(rua_num)
    if seller.complemento:
        partes_endereco.append(str(seller.complemento).strip())
    bairro_cidade: List[str] = []
    if seller.bairro:
        bairro_cidade.append(str(seller.bairro).strip())
    if seller.cidade:
        bairro_cidade.append(str(seller.cidade).strip())
    if bairro_cidade:
        partes_endereco.append(" - ".join(bairro_cidade))
    uf_cep: List[str] = []
    if seller.estado:
        uf_cep.append(str(seller.estado).strip())
    if seller.cep:
        uf_cep.append(str(seller.cep).strip())
    if uf_cep:
        partes_endereco.append(" ".join(uf_cep))

    endereco_completo = ", ".join([p for p in partes_endereco if p])

    info: Dict[str, Any] = {
        "nome_base": base_norm,
        "cnpj": (seller.cnpj or "").strip() or None,
        "endereco_completo": endereco_completo or None,
    }
    return info


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

    # Pacotes G: Saidas com is_grande, agrupar por data e serviço (como cancelados)
    stmt_g = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.is_grande.is_(True),
    )
    if base_norm:
        stmt_g = stmt_g.where(func.upper(Saida.base) == base_key)
    stmt_g = stmt_g.where(
        Saida.data >= periodo_inicio,
        Saida.data <= periodo_fim,
    )
    rows_g = db.scalars(stmt_g).all()
    mapa_g: dict[str, dict] = {}
    for g in rows_g:
        dia = (g.data or g.timestamp.date()).isoformat()
        tipo = _normalizar_servico_saida(g.servico or "")
        if dia not in mapa_g:
            mapa_g[dia] = {"shopee": 0, "ml": 0, "avulso": 0}
        mapa_g[dia][tipo] = mapa_g[dia].get(tipo, 0) + 1

    # Dias únicos (coletas + cancelados + G)
    dias_set = set(mapa_coletas.keys()) | set(mapa_canc.keys()) | set(mapa_g.keys())
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
        g_map = mapa_g.get(dia, {})
        shopee = c.get("shopee", 0)
        ml = c.get("mercado_livre", 0)
        avulso = c.get("avulso", 0)
        canc_s = canc.get("shopee", 0)
        canc_ml = canc.get("ml", 0)
        canc_a = canc.get("avulso", 0)
        g_s = g_map.get("shopee", 0)
        g_m = g_map.get("ml", 0)
        g_a = g_map.get("avulso", 0)
        pacotes_g = g_s + g_m + g_a

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
            "pacotes_g": pacotes_g,
            "g_shopee": g_s,
            "g_ml": g_m,
            "g_avulso": g_a,
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
    pacotes_g: int = 0
    g_shopee: int = 0
    g_ml: int = 0
    g_avulso: int = 0


class FechamentoItemOut(BaseModel):
    data: str
    shopee: int
    mercado_livre: int
    avulso: int
    cancelados_shopee: int
    cancelados_ml: int
    cancelados_avulso: int
    pacotes_g: int = 0
    g_shopee: int = 0
    g_ml: int = 0
    g_avulso: int = 0


class FechamentoCreate(BaseModel):
    base: str = Field(min_length=1)
    periodo_inicio: date
    periodo_fim: date
    itens: Optional[List[FechamentoItemIn]] = None
    valor_adicao: Optional[Decimal] = None
    motivo_adicao: Optional[str] = None
    valor_subtracao: Optional[Decimal] = None
    motivo_subtracao: Optional[str] = None
    ajuste_g_valor: Optional[Decimal] = None
    ajuste_g_motivo: Optional[str] = None


class FechamentoUpdate(BaseModel):
    itens: List[FechamentoItemIn]
    valor_adicao: Optional[Decimal] = None
    motivo_adicao: Optional[str] = None
    valor_subtracao: Optional[Decimal] = None
    motivo_subtracao: Optional[str] = None


class CalcularOut(BaseModel):
    base: str
    periodo_inicio: str
    periodo_fim: str
    valor_bruto: Decimal
    valor_cancelados: Decimal
    valor_final: Decimal
    valor_final_com_ajuste_g: Optional[Decimal] = None
    itens: List[FechamentoItemOut]
    precos: dict
    total_g_shopee: int = 0
    total_g_ml: int = 0
    total_g_avulso: int = 0
    total_pacotes_g: int = 0
    ajuste_g_valor: Optional[Decimal] = None
    ajuste_g_motivo: Optional[str] = None
    seller_info: Optional[Dict[str, Any]] = None


class FechamentoOut(BaseModel):
    id_fechamento: int
    sub_base: str
    base: str
    periodo_inicio: date
    periodo_fim: date
    valor_bruto: Decimal
    valor_cancelados: Decimal
    valor_adicao: Decimal = Decimal("0.00")
    motivo_adicao: Optional[str] = None
    valor_subtracao: Decimal = Decimal("0.00")
    motivo_subtracao: Optional[str] = None
    valor_final: Decimal
    status: str
    criado_em: Optional[datetime] = None
    itens: List[FechamentoItemOut]
    total_g_shopee: int = 0
    total_g_ml: int = 0
    total_g_avulso: int = 0
    total_pacotes_g: int = 0
    divergencia_valor: Optional[bool] = None
    valor_bruto_recalculado: Optional[Decimal] = None
    valor_cancelados_recalculado: Optional[Decimal] = None
    valor_final_recalculado: Optional[Decimal] = None
    seller_info: Optional[Dict[str, Any]] = None


# =========================================================
# GET — Verificar se existe fechamento para base+período
# =========================================================

class VerificarOut(BaseModel):
    existe: bool
    id_fechamento: Optional[int] = None
    status: Optional[str] = None


@router.get("/verificar", response_model=VerificarOut)
def verificar_fechamento_existente(
    base: str = Query(..., min_length=1),
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retorna se já existe fechamento para base+período (para perguntar reajuste)."""
    sub_base = _sub_base_from_token_or_422(current_user)
    base_norm = base.strip()
    base_key = base_norm.upper()
    existente = db.scalar(
        select(BaseFechamento).where(
            BaseFechamento.sub_base == sub_base,
            func.upper(BaseFechamento.base) == base_key,
            BaseFechamento.periodo_inicio == periodo_inicio,
            BaseFechamento.periodo_fim == periodo_fim,
        )
    )
    if not existente:
        return VerificarOut(existe=False)
    st = (existente.status or "GERADO").upper()
    if st == "FECHADO":
        st = "GERADO"
    return VerificarOut(existe=True, id_fechamento=existente.id_fechamento, status=st)


# =========================================================
# GET — Calcular (preview)
# =========================================================

@router.get("/calcular", response_model=CalcularOut)
def calcular_fechamento(
    base: str = Query(..., min_length=1),
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    ajuste_g_valor: Optional[Decimal] = Query(None),
    ajuste_g_motivo: Optional[str] = Query(None),
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

    total_g_shopee = sum(x.get("g_shopee", 0) for x in itens)
    total_g_ml = sum(x.get("g_ml", 0) for x in itens)
    total_g_avulso = sum(x.get("g_avulso", 0) for x in itens)
    total_pacotes_g = sum(x.get("pacotes_g", 0) for x in itens)

    # Ajuste específico de pacotes G (preview apenas)
    ajuste_g_val = _decimal(ajuste_g_valor) if ajuste_g_valor is not None else Decimal("0.00")
    valor_final_com_ajuste_g: Optional[Decimal]
    if ajuste_g_valor is not None:
        valor_final_com_ajuste_g = (valor_final + ajuste_g_val).quantize(Decimal("0.01"))
    else:
        valor_final_com_ajuste_g = None

    seller_info = _montar_seller_info(db, sub_base, base.strip())

    return CalcularOut(
        base=base.strip(),
        periodo_inicio=periodo_inicio.isoformat(),
        periodo_fim=periodo_fim.isoformat(),
        valor_bruto=valor_bruto,
        valor_cancelados=valor_cancelados,
        valor_final=valor_final,
        valor_final_com_ajuste_g=valor_final_com_ajuste_g,
        itens=[FechamentoItemOut(**x) for x in itens],
        precos=precos,
        total_g_shopee=total_g_shopee,
        total_g_ml=total_g_ml,
        total_g_avulso=total_g_avulso,
        total_pacotes_g=total_pacotes_g,
        ajuste_g_valor=ajuste_g_valor,
        ajuste_g_motivo=(ajuste_g_motivo or None),
        seller_info=seller_info,
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
        valor_final_calc = (valor_bruto - valor_cancelados).quantize(Decimal("0.01"))
    else:
        itens_data, valor_bruto, valor_cancelados, valor_final_calc = _build_itens_e_valores(
            db, sub_base, base_norm, payload.periodo_inicio, payload.periodo_fim
        )
        itens_data = [FechamentoItemIn(**x) for x in itens_data]

    # Ajustes manuais genéricos
    valor_ad_base = _decimal(payload.valor_adicao).quantize(Decimal("0.01"))
    valor_sub_base = _decimal(payload.valor_subtracao).quantize(Decimal("0.01"))
    motivo_ad_base = (payload.motivo_adicao or "").strip()
    motivo_sub_base = (payload.motivo_subtracao or "").strip()

    ajuste_g_val_raw = payload.ajuste_g_valor
    ajuste_g_motivo_norm = (payload.ajuste_g_motivo or "").strip()
    valor_ad = valor_ad_base
    valor_sub = valor_sub_base
    motivo_ad_final = motivo_ad_base
    motivo_sub_final = motivo_sub_base

    if ajuste_g_val_raw is not None:
        ajuste_g_val = _decimal(ajuste_g_val_raw).quantize(Decimal("0.01"))
        if ajuste_g_val != 0:
            # Texto padronizado, já incluindo o valor para aparecer claramente no relatório
            ajuste_label = f"[Pacotes G] Motivo: {ajuste_g_motivo_norm or 'Ajuste de pacotes G'}; Valor: R$ {abs(ajuste_g_val):.2f}"
            if ajuste_g_val > 0:
                valor_ad = (valor_ad_base + ajuste_g_val).quantize(Decimal("0.01"))
                motivo_ad_final = " | ".join([m for m in [motivo_ad_base, ajuste_label] if m])
            else:
                incremento_sub = abs(ajuste_g_val)
                valor_sub = (valor_sub_base + incremento_sub).quantize(Decimal("0.01"))
                motivo_sub_final = " | ".join([m for m in [motivo_sub_base, ajuste_label] if m])

    valor_final = (valor_final_calc + valor_ad - valor_sub).quantize(Decimal("0.01"))

    fech = BaseFechamento(
        sub_base=sub_base,
        base=base_norm,
        periodo_inicio=payload.periodo_inicio,
        periodo_fim=payload.periodo_fim,
        valor_bruto=valor_bruto,
        valor_cancelados=valor_cancelados,
        valor_adicao=valor_ad,
        motivo_adicao=(motivo_ad_final or None),
        valor_subtracao=valor_sub,
        motivo_subtracao=(motivo_sub_final or None),
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
            pacotes_g=getattr(it, "pacotes_g", 0) or 0,
            g_shopee=getattr(it, "g_shopee", 0) or 0,
            g_ml=getattr(it, "g_ml", 0) or 0,
            g_avulso=getattr(it, "g_avulso", 0) or 0,
        )
        db.add(item)

    db.commit()
    db.refresh(fech)

    total_g_shopee = sum(getattr(it, "g_shopee", 0) or 0 for it in itens_data)
    total_g_ml = sum(getattr(it, "g_ml", 0) or 0 for it in itens_data)
    total_g_avulso = sum(getattr(it, "g_avulso", 0) or 0 for it in itens_data)
    total_pacotes_g = sum(getattr(it, "pacotes_g", 0) or 0 for it in itens_data)

    itens_out = [
        FechamentoItemOut(
            data=it.data if isinstance(it.data, str) else it.data.isoformat(),
            shopee=it.shopee,
            mercado_livre=it.mercado_livre,
            avulso=it.avulso,
            cancelados_shopee=it.cancelados_shopee,
            cancelados_ml=it.cancelados_ml,
            cancelados_avulso=it.cancelados_avulso,
            pacotes_g=getattr(it, "pacotes_g", 0) or 0,
            g_shopee=getattr(it, "g_shopee", 0) or 0,
            g_ml=getattr(it, "g_ml", 0) or 0,
            g_avulso=getattr(it, "g_avulso", 0) or 0,
        )
        for it in itens_data
    ]

    seller_info = _montar_seller_info(db, sub_base, base_norm)

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        base=fech.base,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_bruto=fech.valor_bruto,
        valor_cancelados=fech.valor_cancelados,
        valor_adicao=fech.valor_adicao,
        motivo_adicao=fech.motivo_adicao,
        valor_subtracao=fech.valor_subtracao,
        motivo_subtracao=fech.motivo_subtracao,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        itens=itens_out,
        total_g_shopee=total_g_shopee,
        total_g_ml=total_g_ml,
        total_g_avulso=total_g_avulso,
        total_pacotes_g=total_pacotes_g,
        seller_info=seller_info,
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
            pacotes_g=getattr(i, "pacotes_g", 0) or 0,
            g_shopee=getattr(i, "g_shopee", 0) or 0,
            g_ml=getattr(i, "g_ml", 0) or 0,
            g_avulso=getattr(i, "g_avulso", 0) or 0,
        )
        for i in itens
    ]

    total_g_shopee = sum(getattr(i, "g_shopee", 0) or 0 for i in itens)
    total_g_ml = sum(getattr(i, "g_ml", 0) or 0 for i in itens)
    total_g_avulso = sum(getattr(i, "g_avulso", 0) or 0 for i in itens)
    total_pacotes_g = sum(getattr(i, "pacotes_g", 0) or 0 for i in itens)

    # Recalcular para detectar divergência (coletas alteradas desde o fechamento)
    divergencia = False
    valor_bruto_rec = valor_cancelados_rec = valor_final_rec = None
    itens_rec, valor_bruto_rec, valor_cancelados_rec, valor_final_rec = _build_itens_e_valores(
        db, sub_base, fech.base, fech.periodo_inicio, fech.periodo_fim
    )
    valor_final_esperado = (fech.valor_bruto - fech.valor_cancelados + (fech.valor_adicao or Decimal("0")) - (fech.valor_subtracao or Decimal("0"))).quantize(Decimal("0.01"))
    if valor_bruto_rec != fech.valor_bruto or valor_cancelados_rec != fech.valor_cancelados:
        divergencia = True
        valor_final_rec = (valor_bruto_rec - valor_cancelados_rec + (fech.valor_adicao or Decimal("0")) - (fech.valor_subtracao or Decimal("0"))).quantize(Decimal("0.01"))

    seller_info = _montar_seller_info(db, sub_base, fech.base)

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        base=fech.base,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_bruto=fech.valor_bruto,
        valor_cancelados=fech.valor_cancelados,
        valor_adicao=getattr(fech, "valor_adicao", None) or Decimal("0.00"),
        motivo_adicao=getattr(fech, "motivo_adicao", None),
        valor_subtracao=getattr(fech, "valor_subtracao", None) or Decimal("0.00"),
        motivo_subtracao=getattr(fech, "motivo_subtracao", None),
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        itens=itens_out,
        total_g_shopee=total_g_shopee,
        total_g_ml=total_g_ml,
        total_g_avulso=total_g_avulso,
        total_pacotes_g=total_pacotes_g,
        divergencia_valor=divergencia if divergencia else None,
        valor_bruto_recalculado=valor_bruto_rec if divergencia else None,
        valor_cancelados_recalculado=valor_cancelados_rec if divergencia else None,
        valor_final_recalculado=valor_final_rec if divergencia else None,
        seller_info=seller_info,
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

    if payload.valor_adicao is not None:
        fech.valor_adicao = Decimal(str(payload.valor_adicao)).quantize(Decimal("0.01"))
    if payload.motivo_adicao is not None:
        fech.motivo_adicao = (payload.motivo_adicao or "").strip() or None
    if payload.valor_subtracao is not None:
        fech.valor_subtracao = Decimal(str(payload.valor_subtracao)).quantize(Decimal("0.01"))
    if payload.motivo_subtracao is not None:
        fech.motivo_subtracao = (payload.motivo_subtracao or "").strip() or None

    valor_ad = getattr(fech, "valor_adicao", None) or Decimal("0.00")
    valor_sub = getattr(fech, "valor_subtracao", None) or Decimal("0.00")
    valor_final = (valor_bruto - valor_cancelados + valor_ad - valor_sub).quantize(Decimal("0.01"))

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
            pacotes_g=getattr(it, "pacotes_g", 0) or 0,
            g_shopee=getattr(it, "g_shopee", 0) or 0,
            g_ml=getattr(it, "g_ml", 0) or 0,
            g_avulso=getattr(it, "g_avulso", 0) or 0,
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
            pacotes_g=getattr(i, "pacotes_g", 0) or 0,
            g_shopee=getattr(i, "g_shopee", 0) or 0,
            g_ml=getattr(i, "g_ml", 0) or 0,
            g_avulso=getattr(i, "g_avulso", 0) or 0,
        )
        for i in itens
    ]
    total_g_shopee = sum(getattr(i, "g_shopee", 0) or 0 for i in itens)
    total_g_ml = sum(getattr(i, "g_ml", 0) or 0 for i in itens)
    total_g_avulso = sum(getattr(i, "g_avulso", 0) or 0 for i in itens)
    total_pacotes_g = sum(getattr(i, "pacotes_g", 0) or 0 for i in itens)

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        base=fech.base,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_bruto=fech.valor_bruto,
        valor_cancelados=fech.valor_cancelados,
        valor_adicao=fech.valor_adicao,
        motivo_adicao=fech.motivo_adicao,
        valor_subtracao=fech.valor_subtracao,
        motivo_subtracao=fech.motivo_subtracao,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        itens=itens_out,
        total_g_shopee=total_g_shopee,
        total_g_ml=total_g_ml,
        total_g_avulso=total_g_avulso,
        total_pacotes_g=total_pacotes_g,
    )
