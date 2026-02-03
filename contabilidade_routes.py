"""
Rotas de Contabilidade / Financeiro
GET /contabilidade/resumo — resumo financeiro por período (receita, despesa, lucro, indicadores).
Usa apenas dados consolidados: coletas (receita) e fechamentos GERADO/REAJUSTADO (despesa).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, and_, or_
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import Coleta, Entregador, EntregadorFechamento, Saida, User

router = APIRouter(prefix="/contabilidade", tags=["Contabilidade"])

STATUS_SAIDAS_VALIDOS = ["saiu", "saiu pra entrega", "saiu_pra_entrega", "entregue"]
STATUS_FECHAMENTO_CONTABIL = ("GERADO", "REAJUSTADO", "FECHADO")  # FECHADO = legado


def _resolve_user_base(db: Session, current_user) -> str:
    sb = getattr(current_user, "sub_base", None)
    if sb:
        return sb
    user_id = getattr(current_user, "id", None)
    if user_id is not None:
        u = db.get(User, user_id)
        if u and getattr(u, "sub_base", None):
            return u.sub_base
    uname = getattr(current_user, "username", None)
    if uname:
        u = db.scalars(select(User).where(User.username == uname)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base
    raise HTTPException(status_code=400, detail="sub_base não definida para o usuário.")


def _decimal(v) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


# --------------- Schemas ---------------


class IndicadoresOperacionais(BaseModel):
    total_coletas: int
    total_saidas: int
    ticket_medio_coleta: Decimal
    custo_medio_saida: Decimal
    lucro_por_pacote: Decimal
    taxa_conversao: Decimal  # total_saidas / total_coletas


class ServicoItem(BaseModel):
    servico: str  # shopee | mercado_livre | avulso
    coletas: int
    saidas: int
    receita: Decimal
    despesa: Decimal
    lucro: Decimal
    margem: Decimal


class BaseItem(BaseModel):
    base: str
    receita: Decimal
    despesa: Decimal
    lucro: Decimal
    margem: Decimal


class EntregadorDespesaItem(BaseModel):
    id_entregador: int
    nome: str
    saidas: int
    despesa: Decimal
    percentual: Decimal


class DRELinha(BaseModel):
    label: str
    valor: Optional[Decimal] = None
    detalhes: Optional[List[str]] = None


class ComparacaoPeriodoAnterior(BaseModel):
    receita_anterior: Decimal
    despesa_anterior: Decimal
    lucro_anterior: Decimal
    margem_anterior: Decimal
    variacao_receita_pct: Optional[Decimal] = None  # positivo = aumento
    variacao_despesa_pct: Optional[Decimal] = None
    variacao_lucro_pct: Optional[Decimal] = None
    variacao_margem_pp: Optional[Decimal] = None  # pontos percentuais


class ContabilidadeResumoResponse(BaseModel):
    data_inicio: str
    data_fim: str
    receita_bruta: Decimal
    despesas_totais: Decimal
    lucro_liquido: Decimal
    margem_liquida: Decimal
    indicadores: IndicadoresOperacionais
    analise_por_servico: List[ServicoItem]
    rentabilidade_por_base: List[BaseItem]
    distribuicao_despesas: List[EntregadorDespesaItem]
    dre: List[DRELinha]
    comparacao_periodo_anterior: Optional[ComparacaoPeriodoAnterior] = None
    aviso_apenas_fechamentos_gerados: bool = True
    aviso_pendentes: bool = False
    total_fechamentos_no_periodo: int = 0


# --------------- Lógica ---------------


@router.get("/resumo", response_model=ContabilidadeResumoResponse)
def get_resumo_contabilidade(
    data_inicio: date = Query(..., description="Data inicial do período"),
    data_fim: date = Query(..., description="Data final do período"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if data_inicio > data_fim:
        raise HTTPException(400, "data_inicio deve ser menor ou igual a data_fim.")

    sub_base = _resolve_user_base(db, current_user)
    dt_start = datetime.combine(data_inicio, time.min)
    dt_end = datetime.combine(data_fim, time(23, 59, 59))

    # ---- 1) RECEITA (coletas no período) ----
    stmt_coletas = (
        select(Coleta)
        .where(
            Coleta.sub_base == sub_base,
            Coleta.timestamp >= dt_start,
            Coleta.timestamp <= dt_end,
        )
        .where(
            (Coleta.shopee > 0) | (Coleta.mercado_livre > 0) | (Coleta.avulso > 0) | (Coleta.valor_total > 0)
        )
    )
    rows_coletas = db.scalars(stmt_coletas).all()
    receita_bruta = sum(_decimal(c.valor_total) for c in rows_coletas)
    total_coletas = sum((c.shopee or 0) + (c.mercado_livre or 0) + (c.avulso or 0) for c in rows_coletas)

    # Receita por serviço (proporcional pelo volume da coleta)
    receita_shopee = Decimal("0")
    receita_ml = Decimal("0")
    receita_avulso = Decimal("0")
    coletas_shopee = 0
    coletas_ml = 0
    coletas_avulso = 0
    for c in rows_coletas:
        s, m, a = (c.shopee or 0), (c.mercado_livre or 0), (c.avulso or 0)
        tot = s + m + a
        v = _decimal(c.valor_total)
        if tot > 0:
            receita_shopee += v * Decimal(s) / Decimal(tot)
            receita_ml += v * Decimal(m) / Decimal(tot)
            receita_avulso += v * Decimal(a) / Decimal(tot)
        coletas_shopee += s
        coletas_ml += m
        coletas_avulso += a

    # ---- 2) SAÍDAS (contagem no período) ----
    stmt_saidas = (
        select(Saida)
        .where(
            Saida.sub_base == sub_base,
            Saida.timestamp >= dt_start,
            Saida.timestamp <= dt_end,
            Saida.codigo.isnot(None),
            func.lower(Saida.status).in_(STATUS_SAIDAS_VALIDOS),
        )
    )
    rows_saidas = db.scalars(stmt_saidas).all()
    total_saidas = len(rows_saidas)

    # Saídas por serviço
    saidas_shopee = sum(1 for s in rows_saidas if (s.servico or "").lower() == "shopee")
    saidas_ml = sum(1 for s in rows_saidas if (s.servico or "").lower() in ("mercado livre", "mercado_livre", "ml", "flex"))
    saidas_avulso = total_saidas - saidas_shopee - saidas_ml

    # ---- 3) DESPESA (fechamentos GERADO/REAJUSTADO que intersectam o período) ----
    stmt_fech = (
        select(EntregadorFechamento)
        .where(
            EntregadorFechamento.sub_base == sub_base,
            EntregadorFechamento.periodo_inicio <= data_fim,
            EntregadorFechamento.periodo_fim >= data_inicio,
            func.upper(EntregadorFechamento.status).in_(STATUS_FECHAMENTO_CONTABIL),
        )
    )
    rows_fech = db.scalars(stmt_fech).all()
    despesas_totais = sum(_decimal(f.valor_final) for f in rows_fech)

    # Despesa por entregador
    despesa_por_ent: Dict[int, Decimal] = {}
    for f in rows_fech:
        eid = f.id_entregador
        despesa_por_ent[eid] = despesa_por_ent.get(eid, Decimal("0")) + _decimal(f.valor_final)

    # Despesa por serviço: rateio proporcional às saídas
    if total_saidas > 0:
        despesa_shopee = despesas_totais * Decimal(saidas_shopee) / Decimal(total_saidas)
        despesa_ml = despesas_totais * Decimal(saidas_ml) / Decimal(total_saidas)
        despesa_avulso = despesas_totais * Decimal(saidas_avulso) / Decimal(total_saidas)
    else:
        despesa_shopee = despesa_ml = despesa_avulso = Decimal("0")

    # ---- 4) Lucro e margem ----
    lucro_liquido = receita_bruta - despesas_totais
    margem_liquida = (lucro_liquido / receita_bruta * 100) if receita_bruta else Decimal("0")

    # ---- 5) Indicadores operacionais ----
    ticket_medio = (receita_bruta / total_coletas) if total_coletas else Decimal("0")
    custo_medio = (despesas_totais / total_saidas) if total_saidas else Decimal("0")
    lucro_pacote = (lucro_liquido / total_coletas) if total_coletas else Decimal("0")
    taxa_conv = (Decimal(total_saidas) / total_coletas * 100) if total_coletas else Decimal("0")

    indicadores = IndicadoresOperacionais(
        total_coletas=total_coletas,
        total_saidas=total_saidas,
        ticket_medio_coleta=ticket_medio.quantize(Decimal("0.01")),
        custo_medio_saida=custo_medio.quantize(Decimal("0.01")),
        lucro_por_pacote=lucro_pacote.quantize(Decimal("0.01")),
        taxa_conversao=taxa_conv.quantize(Decimal("0.01")),
    )

    # ---- 6) Análise por serviço ----
    def _margem(rec: Decimal, desp: Decimal) -> Decimal:
        return (Decimal("0") if rec == 0 else ((rec - desp) / rec * 100).quantize(Decimal("0.01")))

    analise_servico = [
        ServicoItem(
            servico="shopee",
            coletas=coletas_shopee,
            saidas=saidas_shopee,
            receita=receita_shopee.quantize(Decimal("0.01")),
            despesa=despesa_shopee.quantize(Decimal("0.01")),
            lucro=(receita_shopee - despesa_shopee).quantize(Decimal("0.01")),
            margem=_margem(receita_shopee, despesa_shopee),
        ),
        ServicoItem(
            servico="mercado_livre",
            coletas=coletas_ml,
            saidas=saidas_ml,
            receita=receita_ml.quantize(Decimal("0.01")),
            despesa=despesa_ml.quantize(Decimal("0.01")),
            lucro=(receita_ml - despesa_ml).quantize(Decimal("0.01")),
            margem=_margem(receita_ml, despesa_ml),
        ),
        ServicoItem(
            servico="avulso",
            coletas=coletas_avulso,
            saidas=saidas_avulso,
            receita=receita_avulso.quantize(Decimal("0.01")),
            despesa=despesa_avulso.quantize(Decimal("0.01")),
            lucro=(receita_avulso - despesa_avulso).quantize(Decimal("0.01")),
            margem=_margem(receita_avulso, despesa_avulso),
        ),
    ]

    # ---- 7) Rentabilidade por base (receita por base; despesa rateada) ----
    base_receita: Dict[str, Decimal] = {}
    for c in rows_coletas:
        b = (c.base or "").strip().upper() or "SEM BASE"
        base_receita[b] = base_receita.get(b, Decimal("0")) + _decimal(c.valor_total)
    if receita_bruta > 0:
        rentabilidade = []
        for base_nome, rec in sorted(base_receita.items(), key=lambda x: -x[1]):
            pct = rec / receita_bruta
            desp_base = (despesas_totais * pct).quantize(Decimal("0.01"))
            lucro_base = (rec - desp_base).quantize(Decimal("0.01"))
            margem_base = _margem(rec, desp_base)
            rentabilidade.append(
                BaseItem(base=base_nome, receita=rec, despesa=desp_base, lucro=lucro_base, margem=margem_base)
            )
        rentabilidade.sort(key=lambda x: -x.lucro)
    else:
        rentabilidade = [BaseItem(base=b, receita=r, despesa=Decimal("0"), lucro=r, margem=Decimal("0")) for b, r in sorted(base_receita.items())]

    # ---- 8) Distribuição de despesas por entregador ----
    ent_ids = list(despesa_por_ent.keys())
    ent_nomes: Dict[int, str] = {}
    if ent_ids:
        for e in db.scalars(select(Entregador).where(Entregador.id_entregador.in_(ent_ids))).all():
            ent_nomes[e.id_entregador] = e.nome or ""
    saidas_por_ent: Dict[int, int] = {}
    for s in rows_saidas:
        if s.entregador_id:
            saidas_por_ent[s.entregador_id] = saidas_por_ent.get(s.entregador_id, 0) + 1
    dist_despesas = []
    for eid, desp in despesa_por_ent.items():
        pct = (desp / despesas_totais * 100).quantize(Decimal("0.01")) if despesas_totais else Decimal("0")
        dist_despesas.append(
            EntregadorDespesaItem(
                id_entregador=eid,
                nome=ent_nomes.get(eid, "—"),
                saidas=saidas_por_ent.get(eid, 0),
                despesa=desp.quantize(Decimal("0.01")),
                percentual=pct,
            )
        )
    dist_despesas.sort(key=lambda x: -x.despesa)

    # ---- 9) DRE simplificado ----
    dre = [
        DRELinha(
            label="RECEITA BRUTA",
            valor=receita_bruta.quantize(Decimal("0.01")),
            detalhes=[
                f"Shopee ({coletas_shopee} × ticket) = {receita_shopee.quantize(Decimal('0.01'))}",
                f"Mercado Livre ({coletas_ml} × ticket) = {receita_ml.quantize(Decimal('0.01'))}",
                f"Avulso ({coletas_avulso} × ticket) = {receita_avulso.quantize(Decimal('0.01'))}",
            ],
        ),
        DRELinha(
            label="(-) DESPESAS OPERACIONAIS",
            valor=despesas_totais.quantize(Decimal("0.01")),
            detalhes=[f"Custo de Entregas ({total_saidas} × {custo_medio.quantize(Decimal('0.01'))}) = {despesas_totais.quantize(Decimal('0.01'))}"],
        ),
        DRELinha(
            label="LUCRO LÍQUIDO",
            valor=lucro_liquido.quantize(Decimal("0.01")),
            detalhes=[f"Margem: {margem_liquida.quantize(Decimal('0.01'))}%"],
        ),
    ]

    # ---- 10) Comparação com período anterior (mesmo número de dias) ----
    comparacao = None
    try:
        delta_dias = (data_fim - data_inicio).days + 1
        prev_fim = data_inicio - timedelta(days=1)
        prev_ini = prev_fim - timedelta(days=delta_dias - 1)
        if prev_ini >= date(2000, 1, 1):
            dt_prev_start = datetime.combine(prev_ini, time.min)
            dt_prev_end = datetime.combine(prev_fim, time(23, 59, 59))
            rows_c_prev = db.scalars(
                select(Coleta).where(
                    Coleta.sub_base == sub_base,
                    Coleta.timestamp >= dt_prev_start,
                    Coleta.timestamp <= dt_prev_end,
                ).where(
                    (Coleta.shopee > 0) | (Coleta.mercado_livre > 0) | (Coleta.avulso > 0) | (Coleta.valor_total > 0)
                )
            ).all()
            rows_f_prev = db.scalars(
                select(EntregadorFechamento).where(
                    EntregadorFechamento.sub_base == sub_base,
                    EntregadorFechamento.periodo_inicio <= prev_fim,
                    EntregadorFechamento.periodo_fim >= prev_ini,
                    func.upper(EntregadorFechamento.status).in_(STATUS_FECHAMENTO_CONTABIL),
                )
            ).all()
            rec_ant = sum(_decimal(c.valor_total) for c in rows_c_prev)
            desp_ant = sum(_decimal(f.valor_final) for f in rows_f_prev)
            lucro_ant = rec_ant - desp_ant
            margem_ant = (lucro_ant / rec_ant * 100) if rec_ant else Decimal("0")
            v_rec = (receita_bruta - rec_ant) / rec_ant * 100 if rec_ant else None
            v_desp = (despesas_totais - desp_ant) / desp_ant * 100 if desp_ant else None
            v_lucro = (lucro_liquido - lucro_ant) / lucro_ant * 100 if lucro_ant else None
            v_margem = (margem_liquida - margem_ant) if margem_ant is not None else None
            comparacao = ComparacaoPeriodoAnterior(
                receita_anterior=rec_ant.quantize(Decimal("0.01")),
                despesa_anterior=desp_ant.quantize(Decimal("0.01")),
                lucro_anterior=lucro_ant.quantize(Decimal("0.01")),
                margem_anterior=margem_ant.quantize(Decimal("0.01")) if isinstance(margem_ant, Decimal) else Decimal("0"),
                variacao_receita_pct=v_rec.quantize(Decimal("0.01")) if v_rec is not None else None,
                variacao_despesa_pct=v_desp.quantize(Decimal("0.01")) if v_desp is not None else None,
                variacao_lucro_pct=v_lucro.quantize(Decimal("0.01")) if v_lucro is not None else None,
                variacao_margem_pp=v_margem.quantize(Decimal("0.01")) if v_margem is not None else None,
            )
    except Exception:
        comparacao = None

    # ---- 11) Avisos ----
    aviso_pendentes = total_saidas > 0 and len(rows_fech) == 0

    return ContabilidadeResumoResponse(
        data_inicio=data_inicio.isoformat(),
        data_fim=data_fim.isoformat(),
        receita_bruta=receita_bruta.quantize(Decimal("0.01")),
        despesas_totais=despesas_totais.quantize(Decimal("0.01")),
        lucro_liquido=lucro_liquido.quantize(Decimal("0.01")),
        margem_liquida=margem_liquida.quantize(Decimal("0.01")),
        indicadores=indicadores,
        analise_por_servico=analise_servico,
        rentabilidade_por_base=rentabilidade,
        distribuicao_despesas=dist_despesas,
        dre=dre,
        comparacao_periodo_anterior=comparacao,
        aviso_apenas_fechamentos_gerados=True,
        aviso_pendentes=aviso_pendentes,
        total_fechamentos_no_periodo=len(rows_fech),
    )
