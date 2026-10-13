"""
Rotas de Contabilidade / Financeiro
GET /contabilidade/resumo — resumo financeiro por período (receita, despesas confirmadas/pendentes, lucro)
e balanço por status de fechamento (pendente, gerado, recebido/pago).
"""
from __future__ import annotations

import unicodedata
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, and_, or_
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import BaseFechamento, Coleta, Entregador, EntregadorFechamento, Motoboy, Saida, User
from saida_operacional_utils import filtrar_saidas_por_periodo_operacional

from entregador_routes import resolver_precos_entregador, resolver_precos_motoboy, _normalizar_servico
from base_fechamento_routes import _normalizar_servico_saida
from coletas import _get_precos_cached
from contabilidade_balanco_pure import (
    FAIXA_GERADO,
    FAIXA_PAGO,
    FAIXA_RECEBIDO,
    faixa_despesa,
    faixa_receita,
    montar_lado,
    resultado_previsto,
    resultado_realizado,
    variacao_pct,
)

router = APIRouter(prefix="/contabilidade", tags=["Contabilidade"])

# Incluir "saiu_para_entrega" pois o app mobile grava SAIU_PARA_ENTREGA
STATUS_SAIDAS_VALIDOS = ["saiu", "saiu pra entrega", "saiu_pra_entrega", "saiu_para_entrega", "em_rota", "entregue", "ausente", "pendente"]
STATUS_FECHAMENTO_CONTABIL = ("GERADO", "REAJUSTADO", "FECHADO", "PAGO")  # FECHADO = legado


def _saida_conta_para_indicador(saida: Saida, modo_entregas: str) -> bool:
    """
    Define se a saída entra na contagem de indicadores/despesas conforme o modo.
    modo 'saiu'/'operacional': status operacionais válidos (alinhado a Registros/Fechamento).
    modo 'entregue': apenas status exatamente "entregue" (app mobile).
    """
    st = (getattr(saida, "status", None) or "").strip().lower()
    if modo_entregas == "entregue":
        return st == "entregue"
    # modo "saiu"/"operacional" ou default
    return st in STATUS_SAIDAS_VALIDOS


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


def _resolve_actor_saida(db: Session, sub_base: str, saida: Saida) -> Tuple[Optional[int], Optional[int]]:
    """
    Retorna (entregador_id, motoboy_id) da saída. Exatamente um preenchido ou ambos None.
    Prioridade: motoboy_id (app mobile) -> entregador_id -> resolução por nome (saida.entregador).
    """
    if getattr(saida, "motoboy_id", None) is not None:
        return (None, saida.motoboy_id)
    if getattr(saida, "entregador_id", None) is not None:
        return (saida.entregador_id, None)
    nome = (getattr(saida, "entregador", None) or "").strip()
    if not nome:
        return (None, None)
    nome_busca = _normalizar_nome_entregador(nome)
    ent = db.scalar(
        select(Entregador).where(
            Entregador.sub_base == sub_base,
            func.lower(func.unaccent(Entregador.nome)) == nome_busca,
        )
    )
    return (ent.id_entregador, None) if ent else (None, None)


def _get_motoboy_nome(db: Session, motoboy_id: int) -> str:
    """Nome do motoboy (User) para exibição na distribuição de despesas."""
    from motoboy_nome_utils import get_motoboy_display_name
    return get_motoboy_display_name(db, motoboy_id)


def _carregar_nomes_motoboy_ids(db: Session, motoboy_ids: List[int]) -> Dict[int, str]:
    """Resolve nomes de motoboy em lote para evitar N+1."""
    from motoboy_nome_utils import carregar_nomes_motoboy_ids
    return carregar_nomes_motoboy_ids(db, motoboy_ids)


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
    id_entregador: Optional[int] = None
    id_motoboy: Optional[int] = None
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


class BalancoFaixa(BaseModel):
    valor: Decimal
    quantidade: int
    unidade: str  # pacotes | saidas | fechamentos


class BalancoLado(BaseModel):
    pendente: BalancoFaixa
    gerado: BalancoFaixa
    baixado: BalancoFaixa  # recebido (coletas) ou pago (motoboys)
    total: Decimal


class BalancoResultado(BaseModel):
    valor: Decimal
    anterior: Optional[Decimal] = None
    variacao_pct: Optional[Decimal] = None


class Balanco(BaseModel):
    receber: BalancoLado
    pagar: BalancoLado
    previsto: BalancoResultado
    realizado: BalancoResultado
    periodo_anterior_inicio: Optional[str] = None
    periodo_anterior_fim: Optional[str] = None


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
    balanco: Optional[Balanco] = None


# --------------- Lógica ---------------

_PRECOS_ZERADOS = {"shopee_valor": Decimal("0"), "ml_valor": Decimal("0"), "avulso_valor": Decimal("0")}


def _consultar_coletas(db: Session, sub_base: str, data_inicio: date, data_fim: date) -> List[Coleta]:
    dt_start = datetime.combine(data_inicio, time.min)
    dt_end = datetime.combine(data_fim, time(23, 59, 59))
    return db.scalars(
        select(Coleta)
        .where(
            Coleta.sub_base == sub_base,
            Coleta.timestamp >= dt_start,
            Coleta.timestamp <= dt_end,
        )
        .where(
            (Coleta.shopee != 0) | (Coleta.mercado_livre != 0) | (Coleta.avulso != 0) | (Coleta.valor_total != 0)
        )
    ).all()


def _consultar_fechamentos_entregador(
    db: Session, sub_base: str, data_inicio: date, data_fim: date
) -> List[EntregadorFechamento]:
    return db.scalars(
        select(EntregadorFechamento).where(
            EntregadorFechamento.sub_base == sub_base,
            EntregadorFechamento.periodo_inicio <= data_fim,
            EntregadorFechamento.periodo_fim >= data_inicio,
            func.upper(EntregadorFechamento.status).in_(STATUS_FECHAMENTO_CONTABIL),
        )
    ).all()


def _carregar_saidas_periodo(
    db: Session, sub_base: str, data_inicio: date, data_fim: date, modo: str
) -> Tuple[List[Saida], Dict[int, Any], Dict[int, Tuple[Optional[int], Optional[int]]]]:
    """Saídas que contam no período (data operacional e modo) e o ator (entregador ou motoboy) de cada uma."""
    dt_start = datetime.combine(data_inicio, time.min)
    dt_end = datetime.combine(data_fim, time(23, 59, 59))
    rows_saidas_all = db.scalars(
        select(Saida).where(
            Saida.sub_base == sub_base,
            Saida.codigo.isnot(None),
            func.lower(Saida.status).in_(STATUS_SAIDAS_VALIDOS),
            Saida.timestamp >= dt_start,
            Saida.timestamp <= dt_end,
        )
    ).all()
    rows_saidas_raw, op_ctx_map = filtrar_saidas_por_periodo_operacional(db, rows_saidas_all, data_inicio, data_fim)
    rows_saidas = [s for s in rows_saidas_raw if _saida_conta_para_indicador(s, modo)]

    # Resolve ator de cada saída uma única vez para reutilizar em todos os loops.
    nome_legacy_norms = {
        _normalizar_nome_entregador((s.entregador or ""))
        for s in rows_saidas
        if getattr(s, "motoboy_id", None) is None
        and getattr(s, "entregador_id", None) is None
        and (s.entregador or "").strip()
    }
    ent_por_nome_norm: Dict[str, int] = {}
    if nome_legacy_norms:
        rows_ent = db.scalars(select(Entregador).where(Entregador.sub_base == sub_base)).all()
        for ent in rows_ent:
            key = _normalizar_nome_entregador(ent.nome or "")
            if key and key in nome_legacy_norms and key not in ent_por_nome_norm:
                ent_por_nome_norm[key] = ent.id_entregador
    actor_por_saida: Dict[int, Tuple[Optional[int], Optional[int]]] = {}
    for s in rows_saidas:
        eid = getattr(s, "entregador_id", None)
        mid = getattr(s, "motoboy_id", None)
        if mid is not None:
            actor_por_saida[int(s.id_saida)] = (None, int(mid))
            continue
        if eid is not None:
            actor_por_saida[int(s.id_saida)] = (int(eid), None)
            continue
        nome_norm = _normalizar_nome_entregador(s.entregador or "")
        actor_por_saida[int(s.id_saida)] = (ent_por_nome_norm.get(nome_norm), None)
    return rows_saidas, op_ctx_map, actor_por_saida


def _mapa_cobertura_entregador(rows_fech: List[EntregadorFechamento]) -> Dict[tuple, bool]:
    """(("e", entregador_id) ou ("m", motoboy_id), data) -> coberto por fechamento."""
    cache_coberto: Dict[tuple, bool] = {}
    for f in rows_fech:
        d = f.periodo_inicio
        while d <= f.periodo_fim:
            if getattr(f, "id_motoboy", None) is not None:
                cache_coberto[(("m", f.id_motoboy), d)] = True
            else:
                cache_coberto[(("e", f.id_entregador), d)] = True
            d += timedelta(days=1)
    return cache_coberto


def _valor_saida_pendente(
    db: Session,
    sub_base: str,
    eid: Optional[int],
    mid: Optional[int],
    servico: Optional[str],
    cache_precos: Dict[tuple, Dict[str, Decimal]],
) -> Decimal:
    key = ("e", eid) if eid is not None else ("m", mid)
    if key not in cache_precos:
        try:
            if eid is not None:
                cache_precos[key] = resolver_precos_entregador(db, eid, sub_base)
            else:
                cache_precos[key] = resolver_precos_motoboy(db, sub_base)
        except Exception:
            cache_precos[key] = dict(_PRECOS_ZERADOS)
    precos = cache_precos[key]
    tipo = _normalizar_servico(servico)
    if tipo == "shopee":
        return _decimal(precos.get("shopee_valor", 0))
    if tipo == "flex":
        return _decimal(precos.get("ml_valor", 0))
    return _decimal(precos.get("avulso_valor", 0))


def _estimar_despesa_pendente(
    db: Session,
    sub_base: str,
    rows_saidas: List[Saida],
    actor_por_saida: Dict[int, Tuple[Optional[int], Optional[int]]],
    op_ctx_map: Dict[int, Any],
    cache_coberto: Dict[tuple, bool],
    cache_precos: Dict[tuple, Dict[str, Decimal]],
) -> Tuple[Decimal, Dict[int, Decimal], Dict[int, Decimal], int]:
    """Saídas não cobertas por fechamento, ao preço do entregador. Retorna (total, por_entregador, por_motoboy, qtd)."""
    total = Decimal("0")
    por_ent: Dict[int, Decimal] = {}
    por_motoboy: Dict[int, Decimal] = {}
    qtd = 0
    for s in rows_saidas:
        eid, mid = actor_por_saida.get(int(s.id_saida), (None, None))
        if eid is None and mid is None:
            continue
        ctx = op_ctx_map.get(s.id_saida)
        op_ts = (ctx.operacional_ts if ctx and ctx.operacional_ts else None) or s.timestamp
        data_saida = op_ts.date()
        chave_ator = ("e", eid) if eid is not None else ("m", mid)
        if cache_coberto.get((chave_ator, data_saida), False):
            continue
        valor = _valor_saida_pendente(db, sub_base, eid, mid, s.servico, cache_precos)
        total += valor
        qtd += 1
        if eid is not None:
            por_ent[eid] = por_ent.get(eid, Decimal("0")) + valor
        else:
            por_motoboy[mid] = por_motoboy.get(mid, Decimal("0")) + valor
    return total, por_ent, por_motoboy, qtd


def _lado_receber(
    db: Session, sub_base: str, data_inicio: date, data_fim: date, rows_coletas: List[Coleta]
) -> BalancoLado:
    """
    Receita por status do fechamento de base.
    Gerado/recebido: valor_final dos fechamentos que tocam o período.
    Pendente: coletas de base/dia sem fechamento, menos os cancelados dessa base/dia,
    ao preço da base (mesma conta do fechamento, sem ajustes manuais).
    """
    rows_bf = db.scalars(
        select(BaseFechamento).where(
            BaseFechamento.sub_base == sub_base,
            BaseFechamento.periodo_inicio <= data_fim,
            BaseFechamento.periodo_fim >= data_inicio,
        )
    ).all()
    gerado = recebido = Decimal("0")
    qtd_gerado = qtd_recebido = 0
    coberto: set = set()
    for f in rows_bf:
        faixa = faixa_receita(f.status)
        if faixa is None:
            continue
        if faixa == FAIXA_RECEBIDO:
            recebido += _decimal(f.valor_final)
            qtd_recebido += 1
        else:
            gerado += _decimal(f.valor_final)
            qtd_gerado += 1
        base_key = (f.base or "").strip().upper()
        d = max(f.periodo_inicio, data_inicio)
        fim = min(f.periodo_fim, data_fim)
        while d <= fim:
            coberto.add((base_key, d))
            d += timedelta(days=1)

    pendente = Decimal("0")
    qtd_pacotes = 0
    nome_base: Dict[str, str] = {}
    for c in rows_coletas:
        base_key = (c.base or "").strip().upper()
        if (base_key, c.timestamp.date()) in coberto:
            continue
        pendente += _decimal(c.valor_total)
        qtd_pacotes += (c.shopee or 0) + (c.mercado_livre or 0) + (c.avulso or 0)
        if base_key and base_key not in nome_base:
            nome_base[base_key] = (c.base or "").strip()

    from pedido_sincronizado import sincronizado_nao_coletado

    rows_canc = db.scalars(
        select(Saida).where(
            Saida.sub_base == sub_base,
            func.lower(Saida.status) == "cancelado",
            Saida.data >= data_inicio,
            Saida.data <= data_fim,
            ~sincronizado_nao_coletado(),
        )
    ).all()
    precos_base: Dict[str, Tuple[Decimal, Decimal, Decimal]] = {}
    for s in rows_canc:
        base_key = (s.base or "").strip().upper()
        if not base_key:
            continue
        dia = s.data or s.timestamp.date()
        if (base_key, dia) in coberto:
            continue
        if base_key not in precos_base:
            nome = nome_base.get(base_key) or (s.base or "").strip()
            try:
                precos_base[base_key] = _get_precos_cached(db, sub_base, nome)
            except HTTPException:
                precos_base[base_key] = (Decimal("0"), Decimal("0"), Decimal("0"))
        p_shopee, p_ml, p_avulso = precos_base[base_key]
        tipo = _normalizar_servico_saida(s.servico or "")
        pendente -= p_shopee if tipo == "shopee" else p_ml if tipo == "ml" else p_avulso

    valores = montar_lado(pendente, gerado, recebido)
    return BalancoLado(
        pendente=BalancoFaixa(valor=valores["pendente"], quantidade=qtd_pacotes, unidade="pacotes"),
        gerado=BalancoFaixa(valor=valores["gerado"], quantidade=qtd_gerado, unidade="fechamentos"),
        baixado=BalancoFaixa(valor=valores["baixado"], quantidade=qtd_recebido, unidade="fechamentos"),
        total=valores["total"],
    )


def _lado_pagar(rows_fech: List[EntregadorFechamento], pendente: Decimal, qtd_pendente: int) -> BalancoLado:
    gerado = pago = Decimal("0")
    qtd_gerado = qtd_pago = 0
    for f in rows_fech:
        faixa = faixa_despesa(f.status)
        if faixa == FAIXA_PAGO:
            pago += _decimal(f.valor_final)
            qtd_pago += 1
        elif faixa == FAIXA_GERADO:
            gerado += _decimal(f.valor_final)
            qtd_gerado += 1
    valores = montar_lado(pendente, gerado, pago)
    return BalancoLado(
        pendente=BalancoFaixa(valor=valores["pendente"], quantidade=qtd_pendente, unidade="saidas"),
        gerado=BalancoFaixa(valor=valores["gerado"], quantidade=qtd_gerado, unidade="fechamentos"),
        baixado=BalancoFaixa(valor=valores["baixado"], quantidade=qtd_pago, unidade="fechamentos"),
        total=valores["total"],
    )


def _lados_periodo(
    db: Session, sub_base: str, data_inicio: date, data_fim: date, modo: str
) -> Tuple[BalancoLado, BalancoLado]:
    """Balanço completo de um período avulso (usado na comparação com o período anterior)."""
    rows_coletas = _consultar_coletas(db, sub_base, data_inicio, data_fim)
    rows_fech = _consultar_fechamentos_entregador(db, sub_base, data_inicio, data_fim)
    rows_saidas, op_ctx_map, actor_por_saida = _carregar_saidas_periodo(db, sub_base, data_inicio, data_fim, modo)
    pendente, _, _, qtd = _estimar_despesa_pendente(
        db, sub_base, rows_saidas, actor_por_saida, op_ctx_map, _mapa_cobertura_entregador(rows_fech), {}
    )
    return (
        _lado_receber(db, sub_base, data_inicio, data_fim, rows_coletas),
        _lado_pagar(rows_fech, pendente, qtd),
    )


def _montar_balanco(
    receber: BalancoLado,
    pagar: BalancoLado,
    anterior: Optional[Tuple[BalancoLado, BalancoLado]],
    prev_ini: Optional[date],
    prev_fim: Optional[date],
) -> Balanco:
    def _valores(lado: BalancoLado) -> Dict[str, Decimal]:
        return {"total": lado.total, "baixado": lado.baixado.valor}

    previsto = resultado_previsto(_valores(receber), _valores(pagar))
    realizado = resultado_realizado(_valores(receber), _valores(pagar))
    prev_previsto = prev_realizado = None
    if anterior is not None:
        prev_previsto = resultado_previsto(_valores(anterior[0]), _valores(anterior[1]))
        prev_realizado = resultado_realizado(_valores(anterior[0]), _valores(anterior[1]))
    return Balanco(
        receber=receber,
        pagar=pagar,
        previsto=BalancoResultado(
            valor=previsto,
            anterior=prev_previsto,
            variacao_pct=variacao_pct(previsto, prev_previsto) if prev_previsto is not None else None,
        ),
        realizado=BalancoResultado(
            valor=realizado,
            anterior=prev_realizado,
            variacao_pct=variacao_pct(realizado, prev_realizado) if prev_realizado is not None else None,
        ),
        periodo_anterior_inicio=prev_ini.isoformat() if prev_ini else None,
        periodo_anterior_fim=prev_fim.isoformat() if prev_fim else None,
    )


@router.get("/resumo", response_model=ContabilidadeResumoResponse)
def get_resumo_contabilidade(
    data_inicio: date = Query(..., description="Data inicial do período"),
    data_fim: date = Query(..., description="Data final do período"),
    modo_entregas: str = Query("saiu", description="Base de contagem: 'saiu'/'operacional' (status operacionais válidos) ou 'entregue' (status entregue app mobile)"),
    comparar_anterior: bool = Query(
        True,
        description="Calcula o balanço do período anterior para a variação. Com false a resposta sai mais rápido e balanco.*.anterior vem nulo.",
    ),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if data_inicio > data_fim:
        raise HTTPException(400, "data_inicio deve ser menor ou igual a data_fim.")
    modo = (modo_entregas or "saiu").strip().lower()
    if modo not in ("saiu", "operacional", "entregue"):
        modo = "saiu"

    sub_base = getattr(current_user, "sub_base", None)
    if not sub_base:
        raise HTTPException(403, "sub_base não encontrada no token. Faça login novamente.")
    # ---- 1) RECEITA (coletas no período) ----
    rows_coletas = _consultar_coletas(db, sub_base, data_inicio, data_fim)
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
    rows_saidas, op_ctx_map, actor_por_saida = _carregar_saidas_periodo(db, sub_base, data_inicio, data_fim, modo)
    total_saidas = len(rows_saidas)
    nomes_motoboy_map = _carregar_nomes_motoboy_ids(
        db,
        [mid for eid, mid in actor_por_saida.values() if mid is not None],
    )

    # Saídas por serviço
    saidas_shopee = sum(1 for s in rows_saidas if (s.servico or "").lower() == "shopee")
    saidas_ml = sum(1 for s in rows_saidas if (s.servico or "").lower() in ("mercado livre", "mercado_livre", "ml", "flex"))
    saidas_avulso = total_saidas - saidas_shopee - saidas_ml

    # ---- 3) DESPESA CONFIRMADA (fechamentos GERADO/REAJUSTADO/FECHADO/PAGO) ----
    rows_fech = _consultar_fechamentos_entregador(db, sub_base, data_inicio, data_fim)
    despesas_confirmadas = sum(_decimal(f.valor_final) for f in rows_fech)
    cache_coberto = _mapa_cobertura_entregador(rows_fech)

    # ---- 3b) DESPESA PENDENTE (saídas não cobertas por fechamento) ----
    cache_precos: Dict[tuple, Dict[str, Decimal]] = {}  # ("e", eid) ou ("m", mid) -> precos
    (
        despesas_pendentes,
        despesa_pendente_por_ent,
        despesa_pendente_por_motoboy,
        qtd_saidas_pendentes,
    ) = _estimar_despesa_pendente(db, sub_base, rows_saidas, actor_por_saida, op_ctx_map, cache_coberto, cache_precos)

    despesas_pendentes = _decimal(despesas_pendentes).quantize(Decimal("0.01"))
    despesas_totais = _decimal(despesas_confirmadas + despesas_pendentes).quantize(Decimal("0.01"))

    # Despesa por entregador e por motoboy (confirmada + pendente)
    despesa_por_ent: Dict[int, Decimal] = {}
    despesa_por_motoboy: Dict[int, Decimal] = {}
    for f in rows_fech:
        if getattr(f, "id_motoboy", None) is not None:
            mid = f.id_motoboy
            despesa_por_motoboy[mid] = despesa_por_motoboy.get(mid, Decimal("0")) + _decimal(f.valor_final)
        else:
            eid = f.id_entregador
            despesa_por_ent[eid] = despesa_por_ent.get(eid, Decimal("0")) + _decimal(f.valor_final)
    for eid, val in despesa_pendente_por_ent.items():
        despesa_por_ent[eid] = despesa_por_ent.get(eid, Decimal("0")) + _decimal(val)
    for mid, val in despesa_pendente_por_motoboy.items():
        despesa_por_motoboy[mid] = despesa_por_motoboy.get(mid, Decimal("0")) + _decimal(val)

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

    # ---- 8) Distribuição de despesas por entregador e motoboy ----
    ent_ids = list(despesa_por_ent.keys())
    ent_nomes: Dict[int, str] = {}
    if ent_ids:
        for e in db.scalars(select(Entregador).where(Entregador.id_entregador.in_(ent_ids))).all():
            ent_nomes[e.id_entregador] = e.nome or ""
    saidas_por_ent: Dict[int, int] = {}
    saidas_por_motoboy: Dict[int, int] = {}
    for s in rows_saidas:
        eid, mid = actor_por_saida.get(int(s.id_saida), (None, None))
        if eid is not None:
            saidas_por_ent[eid] = saidas_por_ent.get(eid, 0) + 1
        elif mid is not None:
            saidas_por_motoboy[mid] = saidas_por_motoboy.get(mid, 0) + 1
    dist_despesas = []
    desp_tot_dist = _decimal(despesas_totais)
    for eid, desp in despesa_por_ent.items():
        desp_d = _decimal(desp)
        pct = (_decimal(desp_d) / desp_tot_dist * Decimal("100")).quantize(Decimal("0.01")) if desp_tot_dist else Decimal("0")
        dist_despesas.append(
            EntregadorDespesaItem(
                id_entregador=eid,
                id_motoboy=None,
                nome=ent_nomes.get(eid, "—"),
                saidas=saidas_por_ent.get(eid, 0),
                despesa=_decimal(desp_d).quantize(Decimal("0.01")),
                percentual=pct,
            )
        )
    for mid, desp in despesa_por_motoboy.items():
        desp_d = _decimal(desp)
        pct = (_decimal(desp_d) / desp_tot_dist * Decimal("100")).quantize(Decimal("0.01")) if desp_tot_dist else Decimal("0")
        dist_despesas.append(
            EntregadorDespesaItem(
                id_entregador=None,
                id_motoboy=mid,
                nome=nomes_motoboy_map.get(mid, f"Motoboy {mid}"),
                saidas=saidas_por_motoboy.get(mid, 0),
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
        eid, mid = actor_por_saida.get(int(s.id_saida), (None, None))
        if eid is None and mid is None:
            continue
        chave_ator = ("e", eid) if eid is not None else ("m", mid)
        if cache_coberto.get((chave_ator, data_saida), False):
            continue
        valor = _valor_saida_pendente(db, sub_base, eid, mid, s.servico, cache_precos)
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
                    (Coleta.shopee != 0) | (Coleta.mercado_livre != 0) | (Coleta.avulso != 0) | (Coleta.valor_total != 0)
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

    # ---- 10b) Balanço por status (pendente, gerado, recebido/pago) ----
    receber = _lado_receber(db, sub_base, data_inicio, data_fim, rows_coletas)
    pagar = _lado_pagar(rows_fech, despesas_pendentes, qtd_saidas_pendentes)
    delta_dias_bal = (data_fim - data_inicio).days + 1
    bal_prev_fim = data_inicio - timedelta(days=1)
    bal_prev_ini = bal_prev_fim - timedelta(days=delta_dias_bal - 1)
    anterior_bal = None
    if comparar_anterior and bal_prev_ini >= date(2000, 1, 1):
        try:
            anterior_bal = _lados_periodo(db, sub_base, bal_prev_ini, bal_prev_fim, modo)
        except Exception:
            anterior_bal = None
    balanco = _montar_balanco(
        receber,
        pagar,
        anterior_bal,
        bal_prev_ini if anterior_bal is not None else None,
        bal_prev_fim if anterior_bal is not None else None,
    )

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
        balanco=balanco,
    )
