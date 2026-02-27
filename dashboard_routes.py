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
from sqlalchemy import select, func, and_, or_, cast
from sqlalchemy.types import Date
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from base import _resolve_user_sub_base
from models import BasePreco, Coleta, Entregador, Motoboy, Owner, OwnerCobrancaItem, Saida, User

from contabilidade_routes import _get_motoboy_nome as _contab_motoboy_nome, _resolve_actor_saida
from entregador_routes import resolver_precos_entregador, resolver_precos_motoboy, _normalizar_servico

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

# Status considerados válidos (saiu/em rota/entregue/ausente — alinhado ao app mobile)
STATUS_SAIDAS_VALIDOS = ["saiu", "saiu pra entrega", "saiu_pra_entrega", "em_rota", "entregue", "ausente"]
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
    bases_ativas: int
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
    Acesso: ignorar_coleta=false, role 0 ou 1.
    """
    ignorar_coleta = bool(getattr(request.state, "ignorar_coleta", True))
    role = int(getattr(current_user, "role", 99))
    if ignorar_coleta:
        raise HTTPException(
            status_code=403,
            detail="Dashboard Visão 360 disponível apenas para operações com coleta ativa.",
        )
    if role not in (0, 1):
        raise HTTPException(
            status_code=403,
            detail="Acesso restrito a administradores.",
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

    # --- 2. SAÍDAS do período (status válidos: saiu, entregue) — por dia civil ---
    stmt_saidas = (
        select(Saida)
        .where(Saida.sub_base == sub_base)
        .where(Saida.data >= data_inicio)
        .where(Saida.data <= data_fim)
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

    # --- 3b. Bases ativas (cadastro BasePreco ativo) ---
    sub_base_bases = _resolve_user_sub_base(db, current_user)
    stmt_bases = (
        select(BasePreco)
        .where(BasePreco.sub_base == sub_base_bases)
        .where(BasePreco.ativo.is_(True))
        .where(BasePreco.base.isnot(None))
    )
    rows_bases = db.scalars(stmt_bases).all()
    bases_ativas = len(
        {str(b.base).strip().upper() for b in rows_bases if b and b.base and str(b.base).strip()}
    )

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
            .where(Saida.data == d)
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
            bases_ativas=bases_ativas,
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


# =============================================================================
# Dashboard de Coletas — PROMESSA + ORIGEM + VOLUME (apenas coletas)
# Acesso: ignorar_coleta=false, role 0 ou 1
# =============================================================================


class DashboardColetasBasesSemColetasPorDataOut(BaseModel):
    data: str
    bases: List[str]


class DashboardColetasBasesPorDiaOut(BaseModel):
    data: str
    bases_com_coletas: int
    bases_sem_coletas: int
    bases_com_coletas_lista: List[str] = []
    bases_sem_coletas_lista: List[str] = []


class DashboardColetasCardsOut(BaseModel):
    shopee: int
    mercado_livre: int
    avulso: int
    cancelados: int
    total_coletas: int
    valor_total: float
    valor_shopee: float = 0.0
    valor_mercado_livre: float = 0.0
    valor_avulso: float = 0.0
    taxa_cancelamento: float
    bases_total_ativas: int = 0
    bases_com_coletas: int = 0
    bases_sem_coletas: int = 0
    bases_sem_coletas_lista: List[str] = []
    bases_sem_coletas_detalhe: List[DashboardColetasBasesSemColetasPorDataOut] = []
    bases_por_dia: List[DashboardColetasBasesPorDiaOut] = []


class DashboardColetasChartItemOut(BaseModel):
    date: str
    shopee: int
    mercado_livre: int
    avulso: int
    valor_total: float = 0.0


class DashboardColetasRankingBaseOut(BaseModel):
    nome: str
    coletas: int
    shopee: int
    mercado_livre: int
    avulso: int
    valor_total: float
    pct_total: float
    variacao_pct: Optional[float] = None


class DashboardColetasConcentracaoOut(BaseModel):
    top1_base_nome: str
    top1_base_pct: float
    top1_servico_nome: str
    top1_servico_pct: float


class DashboardColetasResponse(BaseModel):
    cards: DashboardColetasCardsOut
    chart_data: List[DashboardColetasChartItemOut]
    ranking_bases: List[DashboardColetasRankingBaseOut]
    concentracao: DashboardColetasConcentracaoOut


@router.get("/coletas", response_model=DashboardColetasResponse)
def get_dashboard_coletas(
    request: Request,
    data_inicio: Optional[date] = Query(None),
    data_fim: Optional[date] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Dashboard de Coletas: promessa, origem e volume.
    Acesso: owner ignorar_coleta=false OU (ignorar_coleta=true e modo_operacao=coleta_manual), role 0 ou 1.
    """
    ignorar_coleta = bool(getattr(request.state, "ignorar_coleta", True))
    modo_operacao = getattr(current_user, "modo_operacao", None) or "codigo"
    role = int(getattr(current_user, "role", 99))
    coleta_ativa = not ignorar_coleta or (ignorar_coleta and modo_operacao == "coleta_manual")
    if not coleta_ativa:
        raise HTTPException(
            status_code=403,
            detail="Dashboard de Coletas disponível apenas para operações com coleta ativa.",
        )
    if role not in (0, 1):
        raise HTTPException(
            status_code=403,
            detail="Acesso restrito a administradores.",
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
    delta_days = (data_fim - data_inicio).days + 1

    # Período anterior (mesmo número de dias)
    data_fim_ant = data_inicio - timedelta(days=1)
    data_inicio_ant = data_fim_ant - timedelta(days=delta_days - 1)
    dt_start_ant = datetime.combine(data_inicio_ant, time.min)
    dt_end_ant = datetime.combine(data_fim_ant, time(23, 59, 59))

    # --- Coletas do período atual ---
    stmt_c = (
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
    rows_coletas = db.execute(stmt_c).scalars().all()

    # --- Coletas do período anterior (para variação) ---
    stmt_c_ant = (
        select(Coleta)
        .where(Coleta.sub_base == sub_base)
        .where(Coleta.timestamp >= dt_start_ant)
        .where(Coleta.timestamp <= dt_end_ant)
        .where(
            (Coleta.shopee > 0)
            | (Coleta.mercado_livre > 0)
            | (Coleta.avulso > 0)
            | (Coleta.valor_total > 0)
        )
    )
    rows_coletas_ant = db.execute(stmt_c_ant).scalars().all()

    # --- Cancelados (tabela Saida) — por dia civil ---
    stmt_cancel = (
        select(Saida)
        .where(Saida.sub_base == sub_base)
        .where(func.lower(Saida.status) == STATUS_CANCELADO)
        .where(Saida.data >= data_inicio)
        .where(Saida.data <= data_fim)
    )
    cancelados_rows = db.execute(stmt_cancel).scalars().all()
    total_cancelados = len(cancelados_rows)

    # --- Totais e cards ---
    shopee = sum(c.shopee or 0 for c in rows_coletas)
    ml = sum(c.mercado_livre or 0 for c in rows_coletas)
    avulso = sum(c.avulso or 0 for c in rows_coletas)
    total_coletas = shopee + ml + avulso
    valor_total = 0.0
    valor_shopee = 0.0
    valor_ml = 0.0
    valor_avulso = 0.0
    for c in rows_coletas:
        vt = float(c.valor_total or 0)
        valor_total += vt
        tq = (c.shopee or 0) + (c.mercado_livre or 0) + (c.avulso or 0)
        if tq > 0 and vt > 0:
            valor_shopee += vt * (c.shopee or 0) / tq
            valor_ml += vt * (c.mercado_livre or 0) / tq
            valor_avulso += vt * (c.avulso or 0) / tq
    taxa_cancelamento = round(
        (total_cancelados / total_coletas * 100), 1
    ) if total_coletas > 0 else 0.0

    # --- Bases ativas: sempre do cadastro (BasePreco ativo), mesma query que GET /base/?status=ativo ---
    sub_base_bases = _resolve_user_sub_base(db, current_user)
    stmt_bases = (
        select(BasePreco)
        .where(BasePreco.sub_base == sub_base_bases)
        .where(BasePreco.ativo.is_(True))
        .where(BasePreco.base.isnot(None))
    )
    rows_bases = db.scalars(stmt_bases).all()
    todas_bases_set = {
        str(b.base).strip().upper()
        for b in rows_bases
        if b and b.base and str(b.base).strip()
    }
    bases_com_coletas_set = {
        (c.base or "").strip().upper() or "S/D"
        for c in rows_coletas
        if (c.base or "").strip()
    }
    bases_sem_coletas_set = todas_bases_set - bases_com_coletas_set
    bases_sem_coletas_lista = sorted(bases_sem_coletas_set)

    # bases_total_ativas = sempre do cadastro (BasePreco ativo), nunca zero "por falta de coleta"
    bases_total_ativas = len(todas_bases_set)

    bases_sem_coletas_detalhe: List[Dict[str, Any]] = []
    bases_por_dia_list: List[Dict[str, Any]] = []
    if delta_days > 1:
        # Por dia: bases com/sem coletas (para gráfico e drill-down)
        bases_por_dia_map: Dict[str, set] = {}
        for c in rows_coletas:
            d = (c.timestamp.date() if hasattr(c.timestamp, "date") else c.timestamp).isoformat()
            b = (c.base or "").strip().upper()
            if b:
                if d not in bases_por_dia_map:
                    bases_por_dia_map[d] = set()
                bases_por_dia_map[d].add(b)
        for d in sorted(
            (data_inicio + timedelta(days=i)).isoformat()
            for i in range(delta_days)
        ):
            com_dia = bases_por_dia_map.get(d, set())
            sem_dia_set = todas_bases_set - com_dia
            sem_dia = sorted(sem_dia_set)
            bases_por_dia_list.append({
                "data": d,
                "bases_com_coletas": len(com_dia),
                "bases_sem_coletas": len(sem_dia_set),
                "bases_com_coletas_lista": sorted(com_dia),
                "bases_sem_coletas_lista": sem_dia,
            })
            if sem_dia:
                bases_sem_coletas_detalhe.append(
                    {"data": d, "bases": sem_dia}
                )

    cards = DashboardColetasCardsOut(
        shopee=shopee,
        mercado_livre=ml,
        avulso=avulso,
        cancelados=total_cancelados,
        total_coletas=total_coletas,
        valor_total=valor_total,
        valor_shopee=valor_shopee,
        valor_mercado_livre=valor_ml,
        valor_avulso=valor_avulso,
        taxa_cancelamento=taxa_cancelamento,
        bases_total_ativas=bases_total_ativas,
        bases_com_coletas=len(bases_com_coletas_set),
        bases_sem_coletas=len(bases_sem_coletas_lista),
        bases_sem_coletas_lista=bases_sem_coletas_lista,
        bases_sem_coletas_detalhe=[
            DashboardColetasBasesSemColetasPorDataOut(data=x["data"], bases=x["bases"])
            for x in bases_sem_coletas_detalhe
        ],
        bases_por_dia=[
            DashboardColetasBasesPorDiaOut(
                data=x["data"],
                bases_com_coletas=x["bases_com_coletas"],
                bases_sem_coletas=x["bases_sem_coletas"],
                bases_com_coletas_lista=x.get("bases_com_coletas_lista", []),
                bases_sem_coletas_lista=x.get("bases_sem_coletas_lista", []),
            )
            for x in bases_por_dia_list
        ],
    )

    # --- Chart por dia ---
    mapa_dia: Dict[str, Dict[str, Any]] = {}
    for c in rows_coletas:
        d = (c.timestamp.date() if hasattr(c.timestamp, "date") else c.timestamp).isoformat()
        if d not in mapa_dia:
            mapa_dia[d] = {"shopee": 0, "mercado_livre": 0, "avulso": 0, "valor_total": 0.0}
        mapa_dia[d]["shopee"] += c.shopee or 0
        mapa_dia[d]["mercado_livre"] += c.mercado_livre or 0
        mapa_dia[d]["avulso"] += c.avulso or 0
        mapa_dia[d]["valor_total"] += float(c.valor_total or 0)

    chart_data = []
    for d in sorted(mapa_dia.keys()):
        v = mapa_dia[d]
        chart_data.append(
            DashboardColetasChartItemOut(
                date=d[8:10] + "/" + d[5:7],
                shopee=v["shopee"],
                mercado_livre=v["mercado_livre"],
                avulso=v["avulso"],
                valor_total=round(v.get("valor_total", 0.0), 2),
            )
        )

    # --- Ranking por base (atual) ---
    base_agg: Dict[str, Dict[str, Any]] = {}
    for c in rows_coletas:
        b = (c.base or "").strip().upper() or "S/D"
        if b not in base_agg:
            base_agg[b] = {"coletas": 0, "shopee": 0, "ml": 0, "avulso": 0, "valor": 0.0}
        base_agg[b]["coletas"] += (c.shopee or 0) + (c.mercado_livre or 0) + (c.avulso or 0)
        base_agg[b]["shopee"] += c.shopee or 0
        base_agg[b]["ml"] += c.mercado_livre or 0
        base_agg[b]["avulso"] += c.avulso or 0
        base_agg[b]["valor"] += float(c.valor_total or 0)

    # --- Ranking por base (anterior, para variação) ---
    base_agg_ant: Dict[str, int] = {}
    for c in rows_coletas_ant:
        b = (c.base or "").strip().upper() or "S/D"
        base_agg_ant[b] = base_agg_ant.get(b, 0) + (c.shopee or 0) + (c.mercado_livre or 0) + (c.avulso or 0)

    ranking_bases = []
    for nome, agg in sorted(base_agg.items(), key=lambda x: -x[1]["coletas"])[:10]:
        coletas_b = agg["coletas"]
        pct_total = round((coletas_b / total_coletas * 100), 0) if total_coletas > 0 else 0
        ant = base_agg_ant.get(nome, 0)
        variacao_pct = None
        if ant > 0:
            variacao_pct = round(((coletas_b - ant) / ant * 100), 1)

        ranking_bases.append(
            DashboardColetasRankingBaseOut(
                nome=nome,
                coletas=coletas_b,
                shopee=agg["shopee"],
                mercado_livre=agg["ml"],
                avulso=agg["avulso"],
                valor_total=agg["valor"],
                pct_total=pct_total,
                variacao_pct=variacao_pct,
            )
        )

    # --- Concentração ---
    top1_base_nome = "-"
    top1_base_pct = 0.0
    if ranking_bases:
        top1_base_nome = ranking_bases[0].nome
        top1_base_pct = ranking_bases[0].pct_total

    servicos = [("Shopee", shopee), ("Mercado Livre", ml), ("Avulso", avulso)]
    servicos.sort(key=lambda x: -x[1])
    top1_servico_nome = servicos[0][0] if servicos else "-"
    top1_servico_pct = round(
        (servicos[0][1] / total_coletas * 100), 0
    ) if total_coletas > 0 and servicos else 0.0

    concentracao = DashboardColetasConcentracaoOut(
        top1_base_nome=top1_base_nome,
        top1_base_pct=top1_base_pct,
        top1_servico_nome=top1_servico_nome,
        top1_servico_pct=top1_servico_pct,
    )

    return DashboardColetasResponse(
        cards=cards,
        chart_data=chart_data,
        ranking_bases=ranking_bases,
        concentracao=concentracao,
    )


# =============================================================================
# Dashboard de Saídas — APENAS ENTREGAS (roles 0 e 1, sem checar ignorar_coleta)
# Landing page quando owner tem ignorar_coleta=true
# =============================================================================


def _decimal(v) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


class DashboardSaidasCardsOut(BaseModel):
    total_saidas: int
    custo_total: Decimal
    custo_medio: Decimal
    entregadores_ativos: int
    cancelamentos: int
    taxa_cancelamento: float


class DashboardSaidasPorMarketplaceOut(BaseModel):
    nome: str  # Shopee | Mercado Livre | Avulso
    qty: int
    valor: Decimal
    pct: float


class DashboardSaidasEvolucaoOut(BaseModel):
    date: str
    shopee: int
    mercado_livre: int
    avulso: int
    valor_total: Decimal


class DashboardSaidasRankingEntregadorOut(BaseModel):
    id_entregador: Optional[int] = None
    id_motoboy: Optional[int] = None
    nome: str
    volume: int
    custo: Decimal
    shopee: int
    mercado_livre: int
    avulso: int


class DashboardSaidasRankingBaseOut(BaseModel):
    base: str
    volume: int
    pct: float


class DashboardSaidasResponse(BaseModel):
    cards: DashboardSaidasCardsOut
    por_marketplace: List[DashboardSaidasPorMarketplaceOut]
    evolucao_diaria: List[DashboardSaidasEvolucaoOut]
    ranking_entregadores: List[DashboardSaidasRankingEntregadorOut]
    ranking_bases: List[DashboardSaidasRankingBaseOut]


@router.get("/saidas", response_model=DashboardSaidasResponse)
def get_dashboard_saidas(
    data_inicio: Optional[date] = Query(None, description="Data inicial (YYYY-MM-DD)"),
    data_fim: Optional[date] = Query(None, description="Data final (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dashboard de saídas (entregas). Acesso: roles 0 e 1. Não checa ignorar_coleta."""
    if current_user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito a owners e admins.")
    sub_base = _sub_base(current_user)
    today = date.today()
    if data_inicio is None:
        data_inicio = today - timedelta(days=29)
    if data_fim is None:
        data_fim = today
    if data_inicio > data_fim:
        data_inicio, data_fim = data_fim, data_inicio
    # Saídas válidas (saiu, entregue) e canceladas no período — por dia civil
    stmt_saidas = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.data >= data_inicio,
        Saida.data <= data_fim,
        Saida.codigo.isnot(None),
    )
    rows_saidas = db.execute(stmt_saidas).scalars().all()
    status_lower = [s.lower() for s in STATUS_SAIDAS_VALIDOS]
    rows_validas = [s for s in rows_saidas if (s.status or "").lower() in status_lower]
    cancelamentos = sum(1 for s in rows_saidas if (s.status or "").lower() in ("cancelado", "cancelada"))
    total_saidas = len(rows_validas)
    taxa_cancelamento = round((cancelamentos / (total_saidas + cancelamentos) * 100), 1) if (total_saidas + cancelamentos) > 0 else 0.0

    # Entregadores/motoboys ativos no período (que tiveram ao menos uma saída)
    atores_ativos = set()  # ("e", eid) ou ("m", mid)
    for s in rows_validas:
        eid, mid = _resolve_actor_saida(db, sub_base, s)
        if eid is not None:
            atores_ativos.add(("e", eid))
        elif mid is not None:
            atores_ativos.add(("m", mid))
    entregadores_ativos = len(atores_ativos)

    # Custo por saída (preço entregador ou preço global motoboy)
    cache_precos: Dict[tuple, Dict[str, Decimal]] = {}  # ("e", eid) ou ("m", mid) -> precos
    custo_total = Decimal("0")
    for s in rows_validas:
        eid, mid = _resolve_actor_saida(db, sub_base, s)
        if eid is None and mid is None:
            continue
        key = ("e", eid) if eid is not None else ("m", mid)
        if key not in cache_precos:
            try:
                if eid is not None:
                    cache_precos[key] = resolver_precos_entregador(db, eid, sub_base)
                else:
                    cache_precos[key] = resolver_precos_motoboy(db, sub_base)
            except Exception:
                cache_precos[key] = {"shopee_valor": Decimal("0"), "ml_valor": Decimal("0"), "avulso_valor": Decimal("0")}
        precos = cache_precos[key]
        tipo = _normalizar_servico(s.servico)
        if tipo == "shopee":
            custo_total += _decimal(precos.get("shopee_valor", 0))
        elif tipo == "flex":
            custo_total += _decimal(precos.get("ml_valor", 0))
        else:
            custo_total += _decimal(precos.get("avulso_valor", 0))
    custo_total = custo_total.quantize(Decimal("0.01"))
    custo_medio = (custo_total / total_saidas).quantize(Decimal("0.01")) if total_saidas > 0 else Decimal("0")

    # Por marketplace
    shopee_qty = sum(1 for s in rows_validas if _classify_servico(s.servico) == "shopee")
    ml_qty = sum(1 for s in rows_validas if _classify_servico(s.servico) == "mercado_livre")
    avulso_qty = total_saidas - shopee_qty - ml_qty
    # Valor por marketplace: rateio proporcional ao custo total
    shopee_valor = (custo_total * shopee_qty / total_saidas).quantize(Decimal("0.01")) if total_saidas > 0 else Decimal("0")
    ml_valor = (custo_total * ml_qty / total_saidas).quantize(Decimal("0.01")) if total_saidas > 0 else Decimal("0")
    avulso_valor = custo_total - shopee_valor - ml_valor
    pct_shopee = round(shopee_qty / total_saidas * 100, 1) if total_saidas > 0 else 0.0
    pct_ml = round(ml_qty / total_saidas * 100, 1) if total_saidas > 0 else 0.0
    pct_avulso = round(avulso_qty / total_saidas * 100, 1) if total_saidas > 0 else 0.0
    por_marketplace = [
        DashboardSaidasPorMarketplaceOut(nome="Shopee", qty=shopee_qty, valor=shopee_valor, pct=pct_shopee),
        DashboardSaidasPorMarketplaceOut(nome="Mercado Livre", qty=ml_qty, valor=ml_valor, pct=pct_ml),
        DashboardSaidasPorMarketplaceOut(nome="Avulso", qty=avulso_qty, valor=avulso_valor, pct=pct_avulso),
    ]

    # Evolução diária
    evolucao_map: Dict[str, Dict[str, Any]] = {}
    for d in range((data_fim - data_inicio).days + 1):
        dt = data_inicio + timedelta(days=d)
        key = dt.isoformat()
        evolucao_map[key] = {"date": key, "shopee": 0, "mercado_livre": 0, "avulso": 0, "valor_total": Decimal("0")}
    for s in rows_validas:
        eid, mid = _resolve_actor_saida(db, sub_base, s)
        if eid is None and mid is None:
            continue
        key = ("e", eid) if eid is not None else ("m", mid)
        if key not in cache_precos:
            try:
                if eid is not None:
                    cache_precos[key] = resolver_precos_entregador(db, eid, sub_base)
                else:
                    cache_precos[key] = resolver_precos_motoboy(db, sub_base)
            except Exception:
                cache_precos[key] = {"shopee_valor": Decimal("0"), "ml_valor": Decimal("0"), "avulso_valor": Decimal("0")}
        precos = cache_precos[key]
        tipo = _normalizar_servico(s.servico)
        if tipo == "shopee":
            v = _decimal(precos.get("shopee_valor", 0))
        elif tipo == "flex":
            v = _decimal(precos.get("ml_valor", 0))
        else:
            v = _decimal(precos.get("avulso_valor", 0))
        date_key = (s.data or s.timestamp.date()).isoformat()
        if date_key in evolucao_map:
            if _classify_servico(s.servico) == "shopee":
                evolucao_map[date_key]["shopee"] += 1
            elif _classify_servico(s.servico) == "mercado_livre":
                evolucao_map[date_key]["mercado_livre"] += 1
            else:
                evolucao_map[date_key]["avulso"] += 1
            evolucao_map[date_key]["valor_total"] += v
    evolucao_diaria = [
        DashboardSaidasEvolucaoOut(
            date=v["date"],
            shopee=v["shopee"],
            mercado_livre=v["mercado_livre"],
            avulso=v["avulso"],
            valor_total=v["valor_total"].quantize(Decimal("0.01")),
        )
        for v in sorted(evolucao_map.values(), key=lambda x: x["date"])
    ]

    # Ranking entregadores/motoboys (volume, custo por ator; chave = ("e", eid) ou ("m", mid))
    ent_vol: Dict[tuple, int] = {}
    ent_custo: Dict[tuple, Decimal] = {}
    ent_shopee: Dict[tuple, int] = {}
    ent_ml: Dict[tuple, int] = {}
    ent_avulso: Dict[tuple, int] = {}
    for s in rows_validas:
        eid, mid = _resolve_actor_saida(db, sub_base, s)
        if eid is None and mid is None:
            continue
        actor_key = ("e", eid) if eid is not None else ("m", mid)
        ent_vol[actor_key] = ent_vol.get(actor_key, 0) + 1
        if actor_key not in cache_precos:
            try:
                if eid is not None:
                    cache_precos[actor_key] = resolver_precos_entregador(db, eid, sub_base)
                else:
                    cache_precos[actor_key] = resolver_precos_motoboy(db, sub_base)
            except Exception:
                cache_precos[actor_key] = {"shopee_valor": Decimal("0"), "ml_valor": Decimal("0"), "avulso_valor": Decimal("0")}
        precos = cache_precos[actor_key]
        tipo = _normalizar_servico(s.servico)
        if tipo == "shopee":
            v = _decimal(precos.get("shopee_valor", 0))
            ent_shopee[actor_key] = ent_shopee.get(actor_key, 0) + 1
        elif tipo == "flex":
            v = _decimal(precos.get("ml_valor", 0))
            ent_ml[actor_key] = ent_ml.get(actor_key, 0) + 1
        else:
            v = _decimal(precos.get("avulso_valor", 0))
            ent_avulso[actor_key] = ent_avulso.get(actor_key, 0) + 1
        ent_custo[actor_key] = ent_custo.get(actor_key, Decimal("0")) + v
    ent_nomes: Dict[tuple, str] = {}
    for k in ent_vol:
        if k[0] == "e":
            ent = db.get(Entregador, k[1])
            ent_nomes[k] = (ent.nome or f"ID {k[1]}") if ent else f"ID {k[1]}"
        else:
            ent_nomes[k] = _contab_motoboy_nome(db, k[1])
    ranking_entregadores = [
        DashboardSaidasRankingEntregadorOut(
            id_entregador=actor_key[1] if actor_key[0] == "e" else None,
            id_motoboy=actor_key[1] if actor_key[0] == "m" else None,
            nome=ent_nomes.get(actor_key, ""),
            volume=ent_vol[actor_key],
            custo=ent_custo[actor_key].quantize(Decimal("0.01")),
            shopee=ent_shopee.get(actor_key, 0),
            mercado_livre=ent_ml.get(actor_key, 0),
            avulso=ent_avulso.get(actor_key, 0),
        )
        for actor_key in sorted(ent_vol.keys(), key=lambda x: -ent_vol[x])
    ][:15]

    # Ranking bases (por volume de saídas)
    base_vol: Dict[str, int] = {}
    for s in rows_validas:
        b = (s.base or "").strip() or "(sem base)"
        base_vol[b] = base_vol.get(b, 0) + 1
    total_base = sum(base_vol.values())
    ranking_bases = [
        DashboardSaidasRankingBaseOut(
            base=b,
            volume=v,
            pct=round(v / total_base * 100, 1) if total_base > 0 else 0.0,
        )
        for b, v in sorted(base_vol.items(), key=lambda x: -x[1])
    ][:15]

    cards = DashboardSaidasCardsOut(
        total_saidas=total_saidas,
        custo_total=custo_total,
        custo_medio=custo_medio,
        entregadores_ativos=entregadores_ativos,
        cancelamentos=cancelamentos,
        taxa_cancelamento=taxa_cancelamento,
    )
    return DashboardSaidasResponse(
        cards=cards,
        por_marketplace=por_marketplace,
        evolucao_diaria=evolucao_diaria,
        ranking_entregadores=ranking_entregadores,
        ranking_bases=ranking_bases,
    )


# =============================================================================
# Dashboard Admin — supervisão de todos os owners (apenas role 0)
# =============================================================================


class DashboardAdminCardsOut(BaseModel):
    total_coletas: int
    total_saidas: int
    receita_admin: Decimal
    owners_ativos: int


class DashboardAdminVolumeOwnerOut(BaseModel):
    sub_base: str
    coletas: int
    saidas: int
    receita: Decimal


class DashboardAdminReceitaOwnerOut(BaseModel):
    sub_base: str
    receita: Decimal
    pct: float


class DashboardAdminPerformanceOwnerOut(BaseModel):
    sub_base: str
    tipo: str  # "Coleta" | "Só Saída"
    coletas: int
    saidas: int
    base_cobranca: int
    receita_admin: Decimal


class DashboardAdminResponse(BaseModel):
    cards: DashboardAdminCardsOut
    volume_por_owner: List[DashboardAdminVolumeOwnerOut]
    receita_por_owner: List[DashboardAdminReceitaOwnerOut]
    performance_por_owner: List[DashboardAdminPerformanceOwnerOut]
    owners: List[Dict[str, Any]]  # [{id_owner, username, sub_base}, ...] para o filtro


@router.get("/admin", response_model=DashboardAdminResponse)
def get_dashboard_admin(
    data_inicio: Optional[date] = Query(None),
    data_fim: Optional[date] = Query(None),
    sub_base: Optional[str] = Query(None, description="Filtro por owner (sub_base)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dashboard administrativo. Acesso: apenas role 0."""
    if current_user.role != 0:
        raise HTTPException(403, "Acesso restrito ao administrador.")
    today = date.today()
    if data_inicio is None:
        data_inicio = today
    if data_fim is None:
        data_fim = today
    if data_inicio > data_fim:
        data_inicio, data_fim = data_fim, data_inicio
    dt_min = datetime.combine(data_inicio, time.min)
    dt_max = datetime.combine(data_fim, time(23, 59, 59))

    sub_bases_filter = [sub_base] if sub_base and sub_base.strip() else None

    def _decimal(v) -> Decimal:
        try:
            return Decimal(str(v or 0))
        except Exception:
            return Decimal("0")

    # Considera apenas owners ativos e não marcados como teste
    owners = db.scalars(
        select(Owner).where(
            Owner.ativo.is_(True),
            Owner.teste.is_(False),
        )
    ).all()
    if sub_bases_filter:
        owners = [o for o in owners if o.sub_base in sub_bases_filter]
    sub_bases = [o.sub_base for o in owners if o.sub_base]
    if not sub_bases:
        return DashboardAdminResponse(
            cards=DashboardAdminCardsOut(
                total_coletas=0,
                total_saidas=0,
                receita_admin=Decimal("0"),
                owners_ativos=0,
            ),
            volume_por_owner=[],
            receita_por_owner=[],
            performance_por_owner=[],
            owners=[
                {"id_owner": o.id_owner, "username": o.username, "sub_base": o.sub_base}
                for o in db.scalars(
                    select(Owner).where(
                        Owner.ativo.is_(True),
                        Owner.teste.is_(False),
                    )
                ).all()
            ],
        )

    stmt_coletas = select(Coleta).where(
        Coleta.sub_base.in_(sub_bases),
        Coleta.timestamp >= dt_min,
        Coleta.timestamp <= dt_max,
    ).where(
        (Coleta.shopee > 0) | (Coleta.mercado_livre > 0) | (Coleta.avulso > 0) | (Coleta.valor_total > 0)
    )
    rows_coletas = db.execute(stmt_coletas).scalars().all()
    total_coletas = len(rows_coletas)

    stmt_saidas = select(Saida).where(
        Saida.sub_base.in_(sub_bases),
        Saida.data >= data_inicio,
        Saida.data <= data_fim,
        Saida.codigo.isnot(None),
        func.lower(Saida.status).in_(STATUS_SAIDAS_VALIDOS),
    )
    rows_saidas = db.execute(stmt_saidas).scalars().all()
    total_saidas = len(rows_saidas)

    stmt_cob = select(OwnerCobrancaItem).where(
        OwnerCobrancaItem.sub_base.in_(sub_bases),
        cast(OwnerCobrancaItem.timestamp, Date) >= data_inicio,
        cast(OwnerCobrancaItem.timestamp, Date) <= data_fim,
        OwnerCobrancaItem.cancelado.is_(False),
    )
    rows_cob = db.execute(stmt_cob).scalars().all()
    # Usa Decimal("0") como acumulador inicial para evitar int puro quando não houver linhas
    receita_admin_total = sum(
        (_decimal(c.valor) for c in rows_cob),
        start=Decimal("0"),
    ).quantize(Decimal("0.01"))

    owner_map = {o.sub_base: o for o in owners}
    owners_ativos = len([s for s in sub_bases if owner_map.get(s) and owner_map[s].ativo])

    coleta_por_sb: Dict[str, int] = {}
    saida_por_sb: Dict[str, int] = {}
    cob_por_sb: Dict[str, Decimal] = {}
    cob_count_por_sb: Dict[str, int] = {}
    for sb in sub_bases:
        coleta_por_sb[sb] = sum(1 for c in rows_coletas if c.sub_base == sb)
        saida_por_sb[sb] = sum(1 for s in rows_saidas if s.sub_base == sb)
        # Garante sempre Decimal, mesmo quando não houver cobranças para a sub_base
        cob_por_sb[sb] = sum(
            (_decimal(c.valor) for c in rows_cob if c.sub_base == sb),
            start=Decimal("0"),
        )
        cob_count_por_sb[sb] = sum(1 for c in rows_cob if c.sub_base == sb)

    volume_por_owner = [
        DashboardAdminVolumeOwnerOut(
            sub_base=sb,
            coletas=coleta_por_sb.get(sb, 0),
            saidas=saida_por_sb.get(sb, 0),
            receita=cob_por_sb.get(sb, Decimal("0")).quantize(Decimal("0.01")),
        )
        for sb in sub_bases
    ]

    receita_por_owner = sorted(
        [DashboardAdminReceitaOwnerOut(
            sub_base=sb,
            receita=cob_por_sb.get(sb, Decimal("0")).quantize(Decimal("0.01")),
            pct=round(float(cob_por_sb.get(sb, 0) / receita_admin_total * 100), 1) if receita_admin_total else 0.0,
        ) for sb in sub_bases],
        key=lambda x: -float(x.receita),
    )[:5]

    performance_por_owner = []
    for sb in sub_bases:
        o = owner_map.get(sb)
        tipo = "Só Saída" if o and o.ignorar_coleta else "Coleta"
        performance_por_owner.append(DashboardAdminPerformanceOwnerOut(
            sub_base=sb,
            tipo=tipo,
            coletas=coleta_por_sb.get(sb, 0),
            saidas=saida_por_sb.get(sb, 0),
            base_cobranca=cob_count_por_sb.get(sb, 0),
            receita_admin=cob_por_sb.get(sb, Decimal("0")).quantize(Decimal("0.01")),
        ))

    owners_list = [
        {"id_owner": o.id_owner, "username": o.username or "", "sub_base": o.sub_base or ""}
        for o in db.scalars(
            select(Owner).where(
                Owner.ativo.is_(True),
                Owner.teste.is_(False),
            )
        ).all()
    ]

    return DashboardAdminResponse(
        cards=DashboardAdminCardsOut(
            total_coletas=total_coletas,
            total_saidas=total_saidas,
            receita_admin=receita_admin_total,
            owners_ativos=owners_ativos,
        ),
        volume_por_owner=volume_por_owner,
        receita_por_owner=receita_por_owner,
        performance_por_owner=performance_por_owner,
        owners=owners_list,
    )
