"""
Rotas de Contabilidade / Financeiro
GET /contabilidade/resumo — resumo financeiro por período (receita, despesas confirmadas/pendentes, lucro).
"""
from __future__ import annotations

import unicodedata
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, and_, or_
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import Coleta, Entregador, EntregadorFechamento, Saida

from entregador_routes import resolver_precos_entregador, _normalizar_servico

router = APIRouter(prefix="/contabilidade", tags=["Contabilidade"])

STATUS_SAIDAS_VALIDOS = ["saiu", "saiu pra entrega", "saiu_pra_entrega", "entregue", "pendente"]
STATUS_FECHAMENTO_CONTABIL = ("GERADO", "REAJUSTADO", "FECHADO")  # FECHADO = legado


def _decimal(v) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def _normalizar_nome_entregador(s: str) -> str:
    """Lower + unaccent para comparação de nome de entregador."""
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _resolve_entregador_id_saida(db: Session, sub_base: str, saida: Saida) -> Optional[int]:
    """
    Retorna o id_entregador efetivo da saída.
    Se saida.entregador_id está preenchido, usa. Senão, tenta resolver pelo nome (saida.entregador).
    """
    if getattr(saida, "entregador_id", None) is not None:
        return saida.entregador_id
    nome = (getattr(saida, "entregador", None) or "").strip()
    if not nome:
        return None
    nome_busca = _normalizar_nome_entregador(nome)
    ent = db.scalar(
        select(Entregador).where(
            Entregador.sub_base == sub_base,
            func.lower(func.unaccent(Entregador.nome)) == nome_busca,
        )
    )
    return ent.id_entregador if ent else None


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


class EvolucaoDiariaItem(BaseModel):
    date: str
    ganhos: Decimal
    despesas: Decimal
    lucro: Decimal


class ContabilidadeResumoResponse(BaseModel):
    data_inicio: str
    data_fim: str
    receita_bruta: Decimal
    despesas_confirmadas: Decimal
    despesas_pendentes: Decimal
    despesas_totais: Decimal
    lucro_liquido: Decimal
    margem_liquida: Decimal
    indicadores: IndicadoresOperacionais
    analise_por_servico: List[ServicoItem]
    rentabilidade_por_base: List[BaseItem]
    distribuicao_despesas: List[EntregadorDespesaItem]
    dre: List[DRELinha]
    comparacao_periodo_anterior: Optional[ComparacaoPeriodoAnterior] = None
    evolucao_diaria: List[EvolucaoDiariaItem] = []
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

    sub_base = getattr(current_user, "sub_base", None)
    if not sub_base:
        raise HTTPException(403, "sub_base não encontrada no token. Faça login novamente.")
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

    # ---- 3) DESPESA CONFIRMADA (fechamentos GERADO/REAJUSTADO/FECHADO) ----
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
    despesas_confirmadas = sum(_decimal(f.valor_final) for f in rows_fech)

    # Cache: (entregador_id, data) -> fechamento cobre
    cache_coberto: Dict[tuple, bool] = {}
    for f in rows_fech:
        d = f.periodo_inicio
        while d <= f.periodo_fim:
            cache_coberto[(f.id_entregador, d)] = True
            d += timedelta(days=1)

    # ---- 3b) DESPESA PENDENTE (saídas não cobertas por fechamento) ----
    # Usa entregador_id da saída; se NULL, resolve pelo nome para incluir registros antigos
    cache_precos: Dict[int, Dict[str, Decimal]] = {}
    despesas_pendentes = Decimal("0")
    despesa_pendente_por_ent: Dict[int, Decimal] = {}
    for s in rows_saidas:
        eid = _resolve_entregador_id_saida(db, sub_base, s)
        if eid is None:
            continue
        data_saida = s.timestamp.date()
        if cache_coberto.get((eid, data_saida), False):
            continue
        if eid not in cache_precos:
            try:
                cache_precos[eid] = resolver_precos_entregador(db, eid, sub_base)
            except Exception:
                cache_precos[eid] = {"shopee_valor": Decimal("0"), "ml_valor": Decimal("0"), "avulso_valor": Decimal("0")}
        precos = cache_precos[eid]
        tipo = _normalizar_servico(s.servico)
        valor = Decimal("0")
        if tipo == "shopee":
            valor = _decimal(precos.get("shopee_valor", 0))
        elif tipo == "flex":
            valor = _decimal(precos.get("ml_valor", 0))
        else:
            valor = _decimal(precos.get("avulso_valor", 0))
        despesas_pendentes += valor
        despesa_pendente_por_ent[eid] = despesa_pendente_por_ent.get(eid, Decimal("0")) + valor

    despesas_pendentes = _decimal(despesas_pendentes).quantize(Decimal("0.01"))
    despesas_totais = _decimal(despesas_confirmadas + despesas_pendentes).quantize(Decimal("0.01"))

    # Despesa por entregador (confirmada + pendente)
    despesa_por_ent: Dict[int, Decimal] = {}
    for f in rows_fech:
        eid = f.id_entregador
        despesa_por_ent[eid] = despesa_por_ent.get(eid, Decimal("0")) + _decimal(f.valor_final)
    for eid, val in despesa_pendente_por_ent.items():
        despesa_por_ent[eid] = despesa_por_ent.get(eid, Decimal("0")) + _decimal(val)

    # Despesa por serviço: rateio proporcional às saídas
    if total_saidas > 0:
        despesa_shopee = despesas_totais * Decimal(saidas_shopee) / Decimal(total_saidas)
        despesa_ml = despesas_totais * Decimal(saidas_ml) / Decimal(total_saidas)
        despesa_avulso = despesas_totais * Decimal(saidas_avulso) / Decimal(total_saidas)
    else:
        despesa_shopee = despesa_ml = despesa_avulso = Decimal("0")

    # ---- 4) Lucro e margem ----
    lucro_liquido = _decimal(receita_bruta - despesas_totais)
    rec_bruta = _decimal(receita_bruta)
    margem_liquida = (_decimal(lucro_liquido) / rec_bruta * Decimal("100")) if rec_bruta else Decimal("0")

    # ---- 5) Indicadores operacionais (garantir Decimal antes de divisão e quantize) ----
    tot_c = _decimal(total_coletas)
    tot_s = _decimal(total_saidas)
    ticket_medio = (_decimal(receita_bruta) / tot_c) if tot_c else Decimal("0")
    custo_medio = (_decimal(despesas_totais) / tot_s) if tot_s else Decimal("0")
    lucro_pacote = (_decimal(lucro_liquido) / tot_c) if tot_c else Decimal("0")
    taxa_conv = (tot_s / tot_c * Decimal("100")) if tot_c else Decimal("0")

    indicadores = IndicadoresOperacionais(
        total_coletas=total_coletas,
        total_saidas=total_saidas,
        ticket_medio_coleta=_decimal(ticket_medio).quantize(Decimal("0.01")),
        custo_medio_saida=_decimal(custo_medio).quantize(Decimal("0.01")),
        lucro_por_pacote=_decimal(lucro_pacote).quantize(Decimal("0.01")),
        taxa_conversao=_decimal(taxa_conv).quantize(Decimal("0.01")),
    )

    # ---- 6) Análise por serviço ----
    def _margem(rec: Decimal, desp: Decimal) -> Decimal:
        r = _decimal(rec)
        if r == 0:
            return Decimal("0")
        return (_decimal(r - desp) / r * Decimal("100")).quantize(Decimal("0.01"))

    analise_servico = [
        ServicoItem(
            servico="shopee",
            coletas=coletas_shopee,
            saidas=saidas_shopee,
            receita=_decimal(receita_shopee).quantize(Decimal("0.01")),
            despesa=_decimal(despesa_shopee).quantize(Decimal("0.01")),
            lucro=_decimal(receita_shopee - despesa_shopee).quantize(Decimal("0.01")),
            margem=_margem(receita_shopee, despesa_shopee),
        ),
        ServicoItem(
            servico="mercado_livre",
            coletas=coletas_ml,
            saidas=saidas_ml,
            receita=_decimal(receita_ml).quantize(Decimal("0.01")),
            despesa=_decimal(despesa_ml).quantize(Decimal("0.01")),
            lucro=_decimal(receita_ml - despesa_ml).quantize(Decimal("0.01")),
            margem=_margem(receita_ml, despesa_ml),
        ),
        ServicoItem(
            servico="avulso",
            coletas=coletas_avulso,
            saidas=saidas_avulso,
            receita=_decimal(receita_avulso).quantize(Decimal("0.01")),
            despesa=_decimal(despesa_avulso).quantize(Decimal("0.01")),
            lucro=_decimal(receita_avulso - despesa_avulso).quantize(Decimal("0.01")),
            margem=_margem(receita_avulso, despesa_avulso),
        ),
    ]

    # ---- 7) Rentabilidade por base (receita por base; despesa rateada) ----
    base_receita: Dict[str, Decimal] = {}
    for c in rows_coletas:
        b = (c.base or "").strip().upper() or "SEM BASE"
        base_receita[b] = base_receita.get(b, Decimal("0")) + _decimal(c.valor_total)
    if _decimal(receita_bruta) > 0:
        rentabilidade = []
        rec_bruta_r = _decimal(receita_bruta)
        desp_tot = _decimal(despesas_totais)
        for base_nome, rec in sorted(base_receita.items(), key=lambda x: -x[1]):
            rec_d = _decimal(rec)
            pct = rec_d / rec_bruta_r
            desp_base = _decimal(desp_tot * pct).quantize(Decimal("0.01"))
            lucro_base = _decimal(rec_d - desp_base).quantize(Decimal("0.01"))
            margem_base = _margem(rec_d, desp_base)
            rentabilidade.append(
                BaseItem(base=base_nome, receita=rec_d, despesa=desp_base, lucro=lucro_base, margem=margem_base)
            )
        rentabilidade.sort(key=lambda x: -x.lucro)
    else:
        rentabilidade = [BaseItem(base=b, receita=_decimal(r), despesa=Decimal("0"), lucro=_decimal(r), margem=Decimal("0")) for b, r in sorted(base_receita.items())]

    # ---- 8) Distribuição de despesas por entregador ----
    ent_ids = list(despesa_por_ent.keys())
    ent_nomes: Dict[int, str] = {}
    if ent_ids:
        for e in db.scalars(select(Entregador).where(Entregador.id_entregador.in_(ent_ids))).all():
            ent_nomes[e.id_entregador] = e.nome or ""
    saidas_por_ent: Dict[int, int] = {}
    for s in rows_saidas:
        eid = _resolve_entregador_id_saida(db, sub_base, s)
        if eid is not None:
            saidas_por_ent[eid] = saidas_por_ent.get(eid, 0) + 1
    dist_despesas = []
    desp_tot_dist = _decimal(despesas_totais)
    for eid, desp in despesa_por_ent.items():
        desp_d = _decimal(desp)
        pct = (_decimal(desp_d) / desp_tot_dist * Decimal("100")).quantize(Decimal("0.01")) if desp_tot_dist else Decimal("0")
        dist_despesas.append(
            EntregadorDespesaItem(
                id_entregador=eid,
                nome=ent_nomes.get(eid, "—"),
                saidas=saidas_por_ent.get(eid, 0),
                despesa=_decimal(desp_d).quantize(Decimal("0.01")),
                percentual=pct,
            )
        )
    dist_despesas.sort(key=lambda x: -x.despesa)

    # ---- 8b) Evolução diária (ganhos, despesas, lucro por dia) ----
    ganhos_por_dia: Dict[date, Decimal] = {}
    for c in rows_coletas:
        d = c.timestamp.date() if hasattr(c.timestamp, "date") else c.data
        ganhos_por_dia[d] = ganhos_por_dia.get(d, Decimal("0")) + _decimal(c.valor_total)

    despesas_por_dia: Dict[date, Decimal] = {}
    for f in rows_fech:
        num_dias = (f.periodo_fim - f.periodo_inicio).days + 1
        if num_dias <= 0:
            continue
        v_por_dia = _decimal(f.valor_final) / Decimal(num_dias)
        d = f.periodo_inicio
        while d <= f.periodo_fim:
            if data_inicio <= d <= data_fim:
                despesas_por_dia[d] = despesas_por_dia.get(d, Decimal("0")) + v_por_dia
            d += timedelta(days=1)
    for s in rows_saidas:
        data_saida = s.timestamp.date()
        eid = _resolve_entregador_id_saida(db, sub_base, s)
        if eid is None:
            continue
        if cache_coberto.get((eid, data_saida), False):
            continue
        if eid not in cache_precos:
            try:
                cache_precos[eid] = resolver_precos_entregador(db, eid, sub_base)
            except Exception:
                cache_precos[eid] = {"shopee_valor": Decimal("0"), "ml_valor": Decimal("0"), "avulso_valor": Decimal("0")}
        precos = cache_precos[eid]
        tipo = _normalizar_servico(s.servico)
        valor = Decimal("0")
        if tipo == "shopee":
            valor = _decimal(precos.get("shopee_valor", 0))
        elif tipo == "flex":
            valor = _decimal(precos.get("ml_valor", 0))
        else:
            valor = _decimal(precos.get("avulso_valor", 0))
        if data_inicio <= data_saida <= data_fim:
            despesas_por_dia[data_saida] = despesas_por_dia.get(data_saida, Decimal("0")) + valor

    evolucao_diaria = []
    d = data_inicio
    while d <= data_fim:
        ganhos = ganhos_por_dia.get(d, Decimal("0"))
        despesas = despesas_por_dia.get(d, Decimal("0"))
        lucro = _decimal(ganhos - despesas)
        evolucao_diaria.append(
            EvolucaoDiariaItem(
                date=d.isoformat(),
                ganhos=_decimal(ganhos).quantize(Decimal("0.01")),
                despesas=_decimal(despesas).quantize(Decimal("0.01")),
                lucro=_decimal(lucro).quantize(Decimal("0.01")),
            )
        )
        d += timedelta(days=1)

    # ---- 9) DRE simplificado ----
    dre = [
        DRELinha(
            label="RECEITA BRUTA",
            valor=_decimal(receita_bruta).quantize(Decimal("0.01")),
            detalhes=[
                f"Shopee ({coletas_shopee} × ticket) = {_decimal(receita_shopee).quantize(Decimal('0.01'))}",
                f"Mercado Livre ({coletas_ml} × ticket) = {_decimal(receita_ml).quantize(Decimal('0.01'))}",
                f"Avulso ({coletas_avulso} × ticket) = {_decimal(receita_avulso).quantize(Decimal('0.01'))}",
            ],
        ),
        DRELinha(
            label="(-) DESPESAS OPERACIONAIS",
            valor=_decimal(despesas_totais).quantize(Decimal("0.01")),
            detalhes=[f"Custo de Entregas ({total_saidas} × {_decimal(custo_medio).quantize(Decimal('0.01'))}) = {_decimal(despesas_totais).quantize(Decimal('0.01'))}"],
        ),
        DRELinha(
            label="LUCRO LÍQUIDO",
            valor=_decimal(lucro_liquido).quantize(Decimal("0.01")),
            detalhes=[f"Margem: {_decimal(margem_liquida).quantize(Decimal('0.01'))}%"],
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
            rec_ant_d = _decimal(rec_ant)
            desp_ant_d = _decimal(desp_ant)
            lucro_ant = _decimal(rec_ant_d - desp_ant_d)
            margem_ant = (lucro_ant / rec_ant_d * Decimal("100")) if rec_ant_d else Decimal("0")
            v_rec = (_decimal(receita_bruta) - rec_ant_d) / rec_ant_d * Decimal("100") if rec_ant_d else None
            v_desp = (_decimal(despesas_totais) - desp_ant_d) / desp_ant_d * Decimal("100") if desp_ant_d else None
            v_lucro = (_decimal(lucro_liquido) - lucro_ant) / lucro_ant * Decimal("100") if lucro_ant else None
            v_margem = _decimal(margem_liquida) - margem_ant if margem_ant is not None else None
            comparacao = ComparacaoPeriodoAnterior(
                receita_anterior=_decimal(rec_ant).quantize(Decimal("0.01")),
                despesa_anterior=_decimal(desp_ant).quantize(Decimal("0.01")),
                lucro_anterior=_decimal(lucro_ant).quantize(Decimal("0.01")),
                margem_anterior=_decimal(margem_ant).quantize(Decimal("0.01")),
                variacao_receita_pct=_decimal(v_rec).quantize(Decimal("0.01")) if v_rec is not None else None,
                variacao_despesa_pct=_decimal(v_desp).quantize(Decimal("0.01")) if v_desp is not None else None,
                variacao_lucro_pct=_decimal(v_lucro).quantize(Decimal("0.01")) if v_lucro is not None else None,
                variacao_margem_pp=_decimal(v_margem).quantize(Decimal("0.01")) if v_margem is not None else None,
            )
    except Exception:
        comparacao = None

    # ---- 11) Avisos ----
    aviso_pendentes = total_saidas > 0 and len(rows_fech) == 0

    return ContabilidadeResumoResponse(
        data_inicio=data_inicio.isoformat(),
        data_fim=data_fim.isoformat(),
        receita_bruta=_decimal(receita_bruta).quantize(Decimal("0.01")),
        despesas_confirmadas=_decimal(despesas_confirmadas).quantize(Decimal("0.01")),
        despesas_pendentes=_decimal(despesas_pendentes).quantize(Decimal("0.01")),
        despesas_totais=_decimal(despesas_totais).quantize(Decimal("0.01")),
        lucro_liquido=_decimal(lucro_liquido).quantize(Decimal("0.01")),
        margem_liquida=_decimal(margem_liquida).quantize(Decimal("0.01")),
        indicadores=indicadores,
        analise_por_servico=analise_servico,
        rentabilidade_por_base=rentabilidade,
        distribuicao_despesas=dist_despesas,
        dre=dre,
        comparacao_periodo_anterior=comparacao,
        evolucao_diaria=evolucao_diaria,
        aviso_apenas_fechamentos_gerados=True,
        aviso_pendentes=aviso_pendentes,
        total_fechamentos_no_periodo=len(rows_fech),
    )
