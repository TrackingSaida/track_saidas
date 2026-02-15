"""
Rotas do Dashboard Visão 360.
GET /dashboard/visao-360 — dados agregados para o dashboard (apenas ignorar_coleta=false).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, func, and_, or_
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import Coleta, Entregador, Saida, User

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

# Status considerados "saiu para entrega" (válidos para taxa de sucesso)
STATUS_SAIDAS_VALIDOS = ["saiu", "saiu pra entrega", "saiu_pra_entrega", "entregue"]
STATUS_CANCELADO = "cancelado"
STATUS_COLETADO = "coletado"

# Média histórica de entregas por rota (fallback quando não há histórico)
MEDIA_ENTREGAS_POR_ROTA_DEFAULT = 140


def _sub_base(user: User) -> str:
    sb = getattr(user, "sub_base", None)
    if not sb:
        raise HTTPException(422, "Usuário sem sub_base definida.")
    return sb


def _deve_exibir_saida(s: Saida) -> bool:
    """Regra: sem base + status saiu/coletado → não exibir. Só exibe sem base se status ≠ coletado e ≠ saiu."""
    base_ok = bool((s.base or "").strip())
    if base_ok:
        return True
    st = (s.status or "").lower().strip()
    if st in ("saiu", "saiu pra entrega", "saiu_pra_entrega", STATUS_COLETADO):
        return False
    return True


def _classify_servico(servico: Optional[str]) -> str:
    s = (servico or "").lower()
    if "shopee" in s:
        return "shopee"
    if "mercado" in s or "ml" in s or "flex" in s:
        return "mercado_livre"
    return "avulso"


# --- Schemas de resposta ---


class StatusOperacionalOut(BaseModel):
    coletas_dia: int
    saidas_dia: int
    entregadores_ativos: int
    cancelamentos_dia: int


class CapacidadeOut(BaseModel):
    demanda: int
    capacidade_calculada: int
    saturacao_pct: float
    media_entregas_por_rota: float


class MarketplaceItemOut(BaseModel):
    nome: str
    coletas: int
    saidas: int
    taxa_aceitacao: float


class AceitacaoOut(BaseModel):
    taxa_aceitacao: float
    por_marketplace: List[MarketplaceItemOut]


class FifoBandOut(BaseModel):
    label: str
    count: int


class FifoPackageOut(BaseModel):
    id: str
    cliente_base: str
    codigo_pacote: str
    marketplace: str
    data_coleta: str
    dias_em_fila: int
    status: str


class FifoOut(BaseModel):
    bands: List[FifoBandOut]
    packages: List[FifoPackageOut]
    marketplaces: List[str]
    total_parados: int


class SlaEstimadoOut(BaseModel):
    taxa_aceitacao: float
    taxa_sucesso_historica: float
    sla_estimado_pct: float


class DailyEvolutionItemOut(BaseModel):
    date: str
    coletas: int
    saidas: int
    taxa_conversao: float


class RankingMotoboyOut(BaseModel):
    id: str
    nome: str
    entregas: int
    nivel: int
    dias_ativos: int
    taxa_sucesso: float


class RankingBaseOut(BaseModel):
    id: str
    nome: str
    coletas: int
    saidas: int
    shopee: int
    mercado_livre: int
    avulso: int


class Visao360Response(BaseModel):
    status_operacional: StatusOperacionalOut
    capacidade: CapacidadeOut
    aceitacao: AceitacaoOut
    gap_aceitacao: int
    fifo: FifoOut
    sla_estimado: SlaEstimadoOut
    daily_evolution: List[DailyEvolutionItemOut]
    ranking_motoboys: List[RankingMotoboyOut]
    ranking_bases: List[RankingBaseOut]


@router.get("/visao-360", response_model=Visao360Response)
def get_visao_360(
    request: Request,
    data_inicio: Optional[date] = Query(None),
    data_fim: Optional[date] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Retorna dados agregados para o dashboard Visão 360.
    Disponível apenas para owners com ignorar_coleta=false.
    """
    ignorar_coleta = bool(getattr(request.state, "ignorar_coleta", True))
    if ignorar_coleta:
        raise HTTPException(
            status_code=403,
            detail="Dashboard Visão 360 disponível apenas para operações com coleta ativa.",
        )

    sub_base = _sub_base(current_user)
    hoje = date.today()

    if data_inicio is None:
        data_inicio = hoje
    if data_fim is None:
        data_fim = hoje
    if data_inicio > data_fim:
        data_inicio, data_fim = data_fim, data_inicio

    dt_start = datetime.combine(data_inicio, time.min)
    dt_end = datetime.combine(data_fim, time(23, 59, 59))

    # Período extendido para evolução diária (últimos 7 dias) e taxa sucesso histórico (30 dias)
    periodo_7d_inicio = hoje - timedelta(days=6)
    periodo_30d_inicio = hoje - timedelta(days=29)

    # --- 1. COLETAS do período ---
    stmt_coletas = (
        select(Coleta)
        .where(Coleta.sub_base == sub_base)
        .where(Coleta.timestamp >= dt_start)
        .where(Coleta.timestamp <= dt_end)
        .where(
            (Coleta.shopee > 0)
            | (Coleta.mercado_livre > 0)
            | (Coleta.avulso > 0)
            | (Coleta.valor_total > 0)
        )
    )
    rows_coletas = db.execute(stmt_coletas).scalars().all()
    total_coletas = sum(
        (c.shopee or 0) + (c.mercado_livre or 0) + (c.avulso or 0) for c in rows_coletas
    )

    # --- 2. SAÍDAS do período (status válidos: saiu, entregue) ---
    stmt_saidas = (
        select(Saida)
        .where(Saida.sub_base == sub_base)
        .where(Saida.timestamp >= dt_start)
        .where(Saida.timestamp <= dt_end)
        .where(Saida.codigo.isnot(None))
    )
    rows_saidas = db.execute(stmt_saidas).scalars().all()
    saidas_validas = [
        s for s in rows_saidas
        if (s.status or "").lower() in STATUS_SAIDAS_VALIDOS and _deve_exibir_saida(s)
    ]
    total_saidas = len(saidas_validas)
    cancelamentos = len([s for s in rows_saidas if (s.status or "").lower() == STATUS_CANCELADO])

    # --- 3. Entregadores ativos ---
    stmt_ent = select(Entregador).where(
        Entregador.sub_base == sub_base,
        Entregador.ativo == True,
    )
    rows_entregadores = db.execute(stmt_ent).scalars().all()
    entregadores_ativos = len(rows_entregadores)

    # --- 4. Aceitação por marketplace ---
    coletas_shopee = sum(c.shopee or 0 for c in rows_coletas)
    coletas_ml = sum(c.mercado_livre or 0 for c in rows_coletas)
    coletas_avulso = sum(c.avulso or 0 for c in rows_coletas)

    saidas_shopee = sum(1 for s in saidas_validas if _classify_servico(s.servico) == "shopee")
    saidas_ml = sum(1 for s in saidas_validas if _classify_servico(s.servico) == "mercado_livre")
    saidas_avulso = sum(1 for s in saidas_validas if _classify_servico(s.servico) == "avulso")

    def _taxa(c: int, s: int) -> float:
        return round((s / c * 100), 1) if c > 0 else 0.0

    por_marketplace = [
        MarketplaceItemOut(
            nome="Shopee",
            coletas=coletas_shopee,
            saidas=saidas_shopee,
            taxa_aceitacao=_taxa(coletas_shopee, saidas_shopee),
        ),
        MarketplaceItemOut(
            nome="Mercado Livre",
            coletas=coletas_ml,
            saidas=saidas_ml,
            taxa_aceitacao=_taxa(coletas_ml, saidas_ml),
        ),
        MarketplaceItemOut(
            nome="Avulso",
            coletas=coletas_avulso,
            saidas=saidas_avulso,
            taxa_aceitacao=_taxa(coletas_avulso, saidas_avulso),
        ),
    ]
    taxa_aceitacao = _taxa(total_coletas, total_saidas)

    # --- 5. Capacidade ---
    media_rota = MEDIA_ENTREGAS_POR_ROTA_DEFAULT
    capacidade = entregadores_ativos * int(media_rota) if entregadores_ativos else 0
    saturacao = round((total_coletas / capacidade * 100), 1) if capacidade > 0 else 0.0

    # --- 6. Taxa sucesso histórico (últimos 30 dias) ---
    stmt_30d = (
        select(Saida)
        .where(Saida.sub_base == sub_base)
        .where(Saida.timestamp >= datetime.combine(periodo_30d_inicio, time.min))
        .where(Saida.timestamp <= datetime.combine(hoje, time.max))
        .where(Saida.codigo.isnot(None))
    )
    rows_30d = db.execute(stmt_30d).scalars().all()
    saidas_30d_validas = [
        s for s in rows_30d
        if (s.status or "").lower() in STATUS_SAIDAS_VALIDOS and _deve_exibir_saida(s)
    ]
    entregues_30d = len([s for s in saidas_30d_validas if (s.status or "").lower() == "entregue"])
    taxa_sucesso_historica = (
        round((entregues_30d / len(saidas_30d_validas) * 100), 1)
        if saidas_30d_validas
        else 99.0
    )

    # SLA estimado: média ponderada aceitação + sucesso (simplificado)
    sla_estimado_pct = round((taxa_aceitacao * 0.5 + taxa_sucesso_historica * 0.5), 1)

    # --- 7. FIFO: pacotes com status coletado ---
    stmt_fifo = (
        select(Saida)
        .where(Saida.sub_base == sub_base)
        .where(func.lower(Saida.status) == STATUS_COLETADO)
        .where(Saida.codigo.isnot(None))
    )
    rows_fifo = db.execute(stmt_fifo).scalars().all()
    hoje_date = date.today()
    bands = [{"label": "D-1", "count": 0}, {"label": "D-2", "count": 0}, {"label": "D-3", "count": 0}, {"label": "≥ D-4", "count": 0}]
    packages: List[Dict[str, Any]] = []
    marketplaces_set: set = set()

    for s in rows_fifo:
        if not _deve_exibir_saida(s):
            continue
        d = (s.timestamp or getattr(s, "data", None)) or s.timestamp
        if hasattr(d, "date"):
            d = d.date()
        elif isinstance(d, datetime):
            d = d.date()
        else:
            d = hoje_date
        dias = (hoje_date - d).days
        if dias < 0:
            dias = 0

        t = _classify_servico(s.servico)
        if t == "shopee":
            marketplaces_set.add("Shopee")
        elif t == "mercado_livre":
            marketplaces_set.add("Mercado Livre")
        else:
            marketplaces_set.add("Avulso")

        if dias <= 1:
            bands[0]["count"] += 1
        elif dias <= 2:
            bands[1]["count"] += 1
        elif dias <= 3:
            bands[2]["count"] += 1
        else:
            bands[3]["count"] += 1

        data_coleta_str = d.strftime("%d/%m") if d else ""
        marketplace_label = "Shopee" if t == "shopee" else ("Mercado Livre" if t == "mercado_livre" else "Avulso")
        status_real = (s.status or "coletado").strip() or "coletado"
        packages.append({
            "id": str(s.id_saida),
            "cliente_base": (s.base or "").strip() or "-",
            "codigo_pacote": (s.codigo or "").strip() or "-",
            "marketplace": marketplace_label,
            "data_coleta": data_coleta_str,
            "dias_em_fila": dias,
            "status": status_real,
        })

    fifo_marketplaces = sorted(marketplaces_set) if marketplaces_set else ["Shopee", "Mercado Livre", "Avulso"]

    # --- 8. Evolução diária (últimos 7 dias) ---
    daily_evolution: List[DailyEvolutionItemOut] = []
    for i in range(7):
        d = periodo_7d_inicio + timedelta(days=i)
        dt_d_start = datetime.combine(d, time.min)
        dt_d_end = datetime.combine(d, time.max)

        stmt_c_d = (
            select(Coleta)
            .where(Coleta.sub_base == sub_base)
            .where(Coleta.timestamp >= dt_d_start)
            .where(Coleta.timestamp <= dt_d_end)
            .where(
                (Coleta.shopee > 0)
                | (Coleta.mercado_livre > 0)
                | (Coleta.avulso > 0)
                | (Coleta.valor_total > 0)
            )
        )
        rows_c_d = db.execute(stmt_c_d).scalars().all()
        c_d = sum((r.shopee or 0) + (r.mercado_livre or 0) + (r.avulso or 0) for r in rows_c_d)

        stmt_s_d = (
            select(Saida)
            .where(Saida.sub_base == sub_base)
            .where(Saida.timestamp >= dt_d_start)
            .where(Saida.timestamp <= dt_d_end)
            .where(Saida.codigo.isnot(None))
            .where(func.lower(Saida.status).in_(STATUS_SAIDAS_VALIDOS))
        )
        rows_s_d = db.execute(stmt_s_d).scalars().all()
        s_d_count = len([r for r in rows_s_d if _deve_exibir_saida(r)])

        taxa_conv = round((s_d_count / c_d * 100), 1) if c_d > 0 else 0.0
        daily_evolution.append(
            DailyEvolutionItemOut(
                date=d.strftime("%d/%m"),
                coletas=c_d,
                saidas=s_d_count,
                taxa_conversao=taxa_conv,
            )
        )

    # --- 9. Ranking motoboys (período atual) ---
    entregador_agg: Dict[str, Dict[str, Any]] = {}  # key: nome normalizado (evita duplicata mesmo id em uns, null em outros)

    for s in saidas_validas:
        nome = (s.entregador or "").strip() or "Sem nome"
        key = (nome.strip().upper() or "S/N").replace("  ", " ")  # nome normalizado como chave única

        if key not in entregador_agg:
            entregador_agg[key] = {
                "nome": nome,
                "entregas": 0,
                "dias": set(),
                "entregues": 0,
            }
        entregador_agg[key]["entregas"] += 1
        if s.timestamp:
            entregador_agg[key]["dias"].add(s.timestamp.date() if hasattr(s.timestamp, "date") else s.timestamp)
        if (s.status or "").lower() == "entregue":
            entregador_agg[key]["entregues"] += 1

    ranking_motoboys = []
    for idx, (key, agg) in enumerate(
        sorted(entregador_agg.items(), key=lambda x: -x[1]["entregas"])[:10]
    ):
        total_e = agg["entregas"]
        entregues_e = agg["entregues"]
        taxa_s = round((entregues_e / total_e * 100), 1) if total_e > 0 else 0
        ranking_motoboys.append(
            RankingMotoboyOut(
                id=key,
                nome=agg["nome"],
                entregas=total_e,
                nivel=min(10, max(1, (idx + 1) * 2)),
                dias_ativos=len(agg["dias"]),
                taxa_sucesso=taxa_s,
            )
        )

    # --- 10. Ranking bases ---
    base_agg: Dict[str, Dict[str, Any]] = {}
    for c in rows_coletas:
        b = (c.base or "").strip().upper() or "S/D"
        if b not in base_agg:
            base_agg[b] = {"coletas": 0, "saidas": 0, "shopee": 0, "ml": 0, "avulso": 0}
        base_agg[b]["coletas"] += (c.shopee or 0) + (c.mercado_livre or 0) + (c.avulso or 0)

    for s in saidas_validas:
        b = (s.base or "").strip().upper() or "S/D"
        if b not in base_agg:
            base_agg[b] = {"coletas": 0, "saidas": 0, "shopee": 0, "ml": 0, "avulso": 0}
        base_agg[b]["saidas"] += 1
        t = _classify_servico(s.servico)
        if t == "shopee":
            base_agg[b]["shopee"] += 1
        elif t == "mercado_livre":
            base_agg[b]["ml"] += 1
        else:
            base_agg[b]["avulso"] += 1

    ranking_bases = []
    for idx, (nome, agg) in enumerate(
        sorted(base_agg.items(), key=lambda x: -(x[1].get("saidas", 0) or x[1].get("coletas", 0)))[:10]
    ):
        saidas_b = agg.get("saidas", 0)
        coletas_b = agg.get("coletas", 0)
        ranking_bases.append(
            RankingBaseOut(
                id=str(idx + 1),
                nome=nome,
                coletas=coletas_b,
                saidas=saidas_b,
                shopee=agg.get("shopee", 0),
                mercado_livre=agg.get("ml", 0),
                avulso=agg.get("avulso", 0),
            )
        )

    return Visao360Response(
        status_operacional=StatusOperacionalOut(
            coletas_dia=total_coletas,
            saidas_dia=total_saidas,
            entregadores_ativos=entregadores_ativos,
            cancelamentos_dia=cancelamentos,
        ),
        capacidade=CapacidadeOut(
            demanda=total_coletas,
            capacidade_calculada=capacidade,
            saturacao_pct=saturacao,
            media_entregas_por_rota=float(media_rota),
        ),
        aceitacao=AceitacaoOut(taxa_aceitacao=taxa_aceitacao, por_marketplace=por_marketplace),
        gap_aceitacao=max(0, total_coletas - total_saidas),
        fifo=FifoOut(
            bands=[FifoBandOut(label=b["label"], count=b["count"]) for b in bands],
            packages=[FifoPackageOut(**p) for p in packages[:20]],
            marketplaces=fifo_marketplaces,
            total_parados=len(packages),
        ),
        sla_estimado=SlaEstimadoOut(
            taxa_aceitacao=taxa_aceitacao,
            taxa_sucesso_historica=taxa_sucesso_historica,
            sla_estimado_pct=sla_estimado_pct,
        ),
        daily_evolution=daily_evolution,
        ranking_motoboys=ranking_motoboys,
        ranking_bases=ranking_bases,
    )
