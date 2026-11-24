"""
Rotas de Acompanhamento do Dia (performance por motoboy por data).
Prefixo: /acompanhamento
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional, List, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from conferencia_saida_pure import filtrar_status_conferencia
from db import get_db
from auth import get_current_user
from models import User, Saida, SaidaDetail, SaidaHistorico, Motoboy, Owner, RotasMotoboy
from saidas_routes import (
    normalizar_status_saida,
    STATUS_SAIU_PARA_ENTREGA,
    STATUS_EM_ROTA,
    STATUS_ENTREGUE,
    STATUS_AUSENTE,
)
from saida_operacional_pure import conta_como_saiu_para_entrega
from saida_operacional_utils import (
    carregar_contexto_operacional,
    carregar_saidas_candidatas_periodo,
    filtrar_saidas_por_periodo_operacional,
)
from acompanhamento_volume_service import (
    calcular_volume_que_entrou,
    contar_saidas_como_indicadores,
)

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
    from motoboy_nome_utils import carregar_nomes_motoboy_ids
    return carregar_nomes_motoboy_ids(db, motoboy_ids)


class ResponsavelResumo(BaseModel):
    motoboy_id: int
    motoboy_nome: str
    pedidos: int


class AcompanhamentoItem(BaseModel):
    data: str
    motoboy_id: Optional[int] = None
    motoboy_nome: Optional[str] = None
    # Prefixo de 3 dígitos do CEP de destino; "SEM_CEP" quando não há CEP.
    regiao: Optional[str] = None
    # Prefixo predominante nos pedidos do motoboy (visão por motoboy).
    cep_predominante: Optional[str] = None
    # Quantidade de motoboys com pacote na região (visão por região).
    responsaveis: Optional[int] = None
    # Lista leve para expand/drawer na visão por região.
    responsaveis_resumo: Optional[List[ResponsavelResumo]] = None
    pedidos: int
    entregues: int
    em_rota: int
    ausente_ou_ocorrencias: int
    rota: str = "—"
    distancia_tempo: Optional[str] = None
    ultima_entrega: Optional[str] = None
    sla: Optional[float] = None
    # Volumes por serviço (saídas do período) — opcionais p/ clientes antigos
    sum_shopee: int = 0
    sum_mercado: int = 0
    sum_avulso: int = 0


class AcompanhamentoTotais(BaseModel):
    pedidos: int
    entregues: int
    em_rota: int
    ausente_ou_ocorrencias: int
    sla: Optional[float] = None
    entrada_habilitada: bool = False
    entradas: Optional[int] = None
    saidas: Optional[int] = None
    pct_saida_sobre_entrada: Optional[float] = None
    aguardando_saida: Optional[int] = None
    outras_situacoes: Optional[int] = None


class AcompanhamentoDiaResponse(BaseModel):
    items: List[AcompanhamentoItem]
    totais: AcompanhamentoTotais
    agrupar: str = "motoboy"
    data_inicio: Optional[str] = None
    data_fim: Optional[str] = None


class AcompanhamentoServicoBreakdown(BaseModel):
    total: int = 0
    pendentes: int = 0
    entregues: int = 0
    ausentes: int = 0


class AcompanhamentoPorServico(BaseModel):
    shopee: AcompanhamentoServicoBreakdown = AcompanhamentoServicoBreakdown()
    mercado_livre: AcompanhamentoServicoBreakdown = AcompanhamentoServicoBreakdown()
    avulso: AcompanhamentoServicoBreakdown = AcompanhamentoServicoBreakdown()


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
    # Breakdown opcional (clientes antigos ignoram).
    por_servico: Optional[AcompanhamentoPorServico] = None
    total_hoje: Optional[int] = None
    entregues_hoje: Optional[int] = None
    ausentes_hoje: Optional[int] = None


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
    agrupar: str = Query("motoboy", description="motoboy | regiao"),
    regiao: Optional[str] = Query(None, description="Filtro de região (3 dígitos) quando agrupar=regiao"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Desempenho do dia: por motoboy (padrão) ou por região (prefixo CEP de destino).
    """
    from saida_regiao_pure import normalizar_filtro_regiao, regiao_de_cep

    sub_base = getattr(current_user, "sub_base", None)
    if not sub_base or not str(sub_base).strip():
        raise HTTPException(status_code=403, detail="Sub_base não definida.")

    modo = (agrupar or "motoboy").strip().lower()
    if modo not in ("motoboy", "regiao"):
        raise HTTPException(400, "agrupar deve ser motoboy ou regiao.")
    regiao_n = normalizar_filtro_regiao(regiao)
    if regiao and str(regiao).strip() and regiao_n is None and str(regiao).strip().upper() != "SEM_CEP":
        raise HTTPException(422, "Região deve ter exatamente 3 dígitos (ou SEM_CEP).")
    if str(regiao or "").strip().upper() == "SEM_CEP":
        regiao_n = "SEM_CEP"

    inicio, fim = _resolver_periodo(data, data_inicio, data_fim)
    label_data = fim.isoformat() if inicio == fim else f"{inicio.isoformat()}_{fim.isoformat()}"

    stmt = (
        select(Saida)
        .where(Saida.sub_base == sub_base)
        .where(Saida.data >= inicio)
        .where(Saida.data <= fim)
    )
    if modo == "motoboy":
        stmt = stmt.where(Saida.motoboy_id.isnot(None))
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

    items: List[AcompanhamentoItem] = []
    totais_pedidos = 0
    totais_entregues = 0
    totais_em_rota = 0
    totais_ausente = 0

    def _contar_status(list_saidas: list) -> Tuple[int, int, int, int, Optional[object]]:
        pedidos = len(list_saidas)
        entregues = 0
        em_rota = 0
        ausente_ou_ocorrencias = 0
        ultima_entrega_dt = None
        for s in list_saidas:
            st = normalizar_status_saida(s.status)
            if st == STATUS_ENTREGUE:
                entregues += 1
            elif st == STATUS_EM_ROTA or conta_como_saiu_para_entrega(s.status):
                em_rota += 1
            elif st == STATUS_AUSENTE:
                ausente_ou_ocorrencias += 1
            ts_entrega = cache_entregue.get(s.id_saida) if s.id_saida in cache_entregue else s.data_hora_entrega
            if ts_entrega:
                if ultima_entrega_dt is None or ts_entrega > ultima_entrega_dt:
                    ultima_entrega_dt = ts_entrega
        return pedidos, entregues, em_rota, ausente_ou_ocorrencias, ultima_entrega_dt

    cep_map: dict[int, Optional[str]] = {}
    if ids_saida:
        for row in db.execute(
            select(SaidaDetail.id_saida, SaidaDetail.dest_cep).where(
                SaidaDetail.id_saida.in_(ids_saida)
            )
        ).all():
            cep_map[int(row[0])] = row[1]

    def _prefix_saida(s: Saida) -> str:
        return regiao_de_cep(cep_map.get(s.id_saida)) or "SEM_CEP"

    def _cep_predominante(list_saidas: list) -> Optional[str]:
        counts: dict[str, int] = {}
        for s in list_saidas:
            prefix = regiao_de_cep(cep_map.get(s.id_saida))
            if not prefix:
                continue
            counts[prefix] = counts.get(prefix, 0) + 1
        if not counts:
            return "SEM_CEP"
        # Empate: menor prefixo
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    if modo == "regiao":
        by_regiao: dict[str, list] = {}
        for s in saidas:
            prefix = _prefix_saida(s)
            if regiao_n and prefix != regiao_n:
                continue
            by_regiao.setdefault(prefix, []).append(s)

        mids_regiao = {
            int(s.motoboy_id)
            for list_saidas in by_regiao.values()
            for s in list_saidas
            if s.motoboy_id is not None
        }
        nomes_regiao = _carregar_nomes_motoboy_ids(db, list(mids_regiao)) if mids_regiao else {}

        for prefix, list_saidas in by_regiao.items():
            pedidos, entregues, em_rota, ausente_ou_ocorrencias, ultima_entrega_dt = _contar_status(
                list_saidas
            )
            sla = round(100.0 * entregues / pedidos, 1) if pedidos > 0 else None
            sum_shopee, sum_mercado, sum_avulso = _somar_servicos(list_saidas)
            por_motoboy: dict[int, int] = {}
            for s in list_saidas:
                if s.motoboy_id is None:
                    continue
                mid = int(s.motoboy_id)
                por_motoboy[mid] = por_motoboy.get(mid, 0) + 1
            resumo = [
                ResponsavelResumo(
                    motoboy_id=mid,
                    motoboy_nome=nomes_regiao.get(mid, f"Motoboy {mid}"),
                    pedidos=qtd,
                )
                for mid, qtd in sorted(por_motoboy.items(), key=lambda kv: (-kv[1], kv[0]))
            ]
            items.append(
                AcompanhamentoItem(
                    data=label_data,
                    motoboy_id=None,
                    motoboy_nome=None,
                    regiao=prefix,
                    responsaveis=len(resumo),
                    responsaveis_resumo=resumo,
                    pedidos=pedidos,
                    entregues=entregues,
                    em_rota=em_rota,
                    ausente_ou_ocorrencias=ausente_ou_ocorrencias,
                    rota="—",
                    distancia_tempo=None,
                    ultima_entrega=ultima_entrega_dt.isoformat() if ultima_entrega_dt else None,
                    sla=sla,
                    sum_shopee=sum_shopee,
                    sum_mercado=sum_mercado,
                    sum_avulso=sum_avulso,
                )
            )
            totais_pedidos += pedidos
            totais_entregues += entregues
            totais_em_rota += em_rota
            totais_ausente += ausente_ou_ocorrencias

        items.sort(
            key=lambda it: (
                1 if it.regiao == "SEM_CEP" else 0,
                it.regiao or "",
            )
        )
    else:
        by_motoboy: dict[int, list] = {}
        for s in saidas:
            mid = s.motoboy_id
            if mid is None:
                continue
            by_motoboy.setdefault(mid, []).append(s)

        rotas_map: dict[int, str] = {}
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

        nomes_motoboy = _carregar_nomes_motoboy_ids(db, [int(mid) for mid in by_motoboy.keys()])
        for mid, list_saidas in by_motoboy.items():
            pedidos, entregues, em_rota, ausente_ou_ocorrencias, ultima_entrega_dt = _contar_status(
                list_saidas
            )
            sla = round(100.0 * entregues / pedidos, 1) if pedidos > 0 else None
            sum_shopee, sum_mercado, sum_avulso = _somar_servicos(list_saidas)
            motoboy_nome = nomes_motoboy.get(int(mid), f"Motoboy {mid}")

            items.append(
                AcompanhamentoItem(
                    data=label_data,
                    motoboy_id=mid,
                    motoboy_nome=motoboy_nome,
                    regiao=None,
                    cep_predominante=_cep_predominante(list_saidas),
                    pedidos=pedidos,
                    entregues=entregues,
                    em_rota=em_rota,
                    ausente_ou_ocorrencias=ausente_ou_ocorrencias,
                    rota=rotas_map.get(mid, "SEM ROTA"),
                    distancia_tempo=None,
                    ultima_entrega=ultima_entrega_dt.isoformat() if ultima_entrega_dt else None,
                    sla=sla,
                    sum_shopee=sum_shopee,
                    sum_mercado=sum_mercado,
                    sum_avulso=sum_avulso,
                )
            )
            totais_pedidos += pedidos
            totais_entregues += entregues
            totais_em_rota += em_rota
            totais_ausente += ausente_ou_ocorrencias

        items.sort(key=lambda it: (it.motoboy_nome or "").casefold())

    sla_total = round(100.0 * totais_entregues / totais_pedidos, 1) if totais_pedidos > 0 else None

    entrada_habilitada = bool(getattr(current_user, "entrada_obrigatoria_habilitada", False))
    if not entrada_habilitada:
        owner_row = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
        entrada_habilitada = bool(
            owner_row and getattr(owner_row, "entrada_obrigatoria_habilitada", False)
        )

    entradas_count: Optional[int] = None
    saidas_count: Optional[int] = None
    pct_saida: Optional[float] = None
    # Volume do dia sempre disponível para o resumo da transportadora (protótipo).
    entradas_count, _total_coletas, _total_entradas = calcular_volume_que_entrou(
        db, sub_base, inicio, fim
    )
    saidas_count = contar_saidas_como_indicadores(db, sub_base, inicio, fim)
    if entradas_count > 0:
        pct_saida = round(100.0 * saidas_count / entradas_count, 1)
    aguardando = max(0, int(entradas_count or 0) - int(saidas_count or 0))
    outras = max(
        0,
        int(saidas_count or 0) - int(totais_entregues) - int(totais_em_rota),
    )

    return AcompanhamentoDiaResponse(
        items=items,
        totais=AcompanhamentoTotais(
            pedidos=totais_pedidos,
            entregues=totais_entregues,
            em_rota=totais_em_rota,
            ausente_ou_ocorrencias=totais_ausente,
            sla=sla_total,
            entrada_habilitada=entrada_habilitada,
            entradas=entradas_count,
            saidas=saidas_count,
            pct_saida_sobre_entrada=pct_saida,
            aguardando_saida=aguardando,
            outras_situacoes=outras,
        ),
        agrupar=modo,
        data_inicio=inicio.isoformat(),
        data_fim=fim.isoformat(),
    )


def _classificar_servico_saida(servico: Optional[str]) -> str:
    srv = (servico or "").strip().lower()
    if ("shopee" in srv) or ("spx" in srv):
        return "shopee"
    if (
        ("mercado livre" in srv)
        or ("mercado_livre" in srv)
        or ("mercadolivre" in srv)
        or (" ml" in f" {srv}")
        or ("flex" in srv)
    ):
        return "mercado"
    return "avulso"


def _somar_servicos(rows: List[Saida]) -> Tuple[int, int, int]:
    sum_shopee = 0
    sum_mercado = 0
    sum_avulso = 0
    for s in rows:
        kind = _classificar_servico_saida(s.servico)
        if kind == "shopee":
            sum_shopee += 1
        elif kind == "mercado":
            sum_mercado += 1
        else:
            sum_avulso += 1
    return sum_shopee, sum_mercado, sum_avulso


def _bucket_status_acompanhamento(status_raw: Optional[str]) -> Optional[str]:
    """pendentes | entregues | ausentes — alinhado ao /acompanhamento/dia."""
    st = normalizar_status_saida(status_raw)
    if st == "AGUARDANDO_CONFIRMACAO_SAIDA":
        return None
    if st == STATUS_ENTREGUE:
        return "entregues"
    if st in (STATUS_EM_ROTA, STATUS_SAIU_PARA_ENTREGA, "saiu"):
        return "pendentes"
    if st == STATUS_AUSENTE:
        return "ausentes"
    return None


def _por_servico_breakdown(rows: List[Saida]) -> AcompanhamentoPorServico:
    buckets = {
        "shopee": {"total": 0, "pendentes": 0, "entregues": 0, "ausentes": 0},
        "mercado_livre": {"total": 0, "pendentes": 0, "entregues": 0, "ausentes": 0},
        "avulso": {"total": 0, "pendentes": 0, "entregues": 0, "ausentes": 0},
    }
    for s in rows:
        kind = _classificar_servico_saida(s.servico)
        key = "mercado_livre" if kind == "mercado" else kind
        if key not in buckets:
            key = "avulso"
        buckets[key]["total"] += 1
        bucket = _bucket_status_acompanhamento(getattr(s, "status", None))
        if bucket:
            buckets[key][bucket] += 1
    return AcompanhamentoPorServico(
        shopee=AcompanhamentoServicoBreakdown(**buckets["shopee"]),
        mercado_livre=AcompanhamentoServicoBreakdown(**buckets["mercado_livre"]),
        avulso=AcompanhamentoServicoBreakdown(**buckets["avulso"]),
    )


@router.get("/saidas-dia", response_model=AcompanhamentoSaidasDiaResponse)
def acompanhamento_saidas_dia(
    motoboy_id: int = Query(..., description="Motoboy obrigatório"),
    data: Optional[date] = Query(None, description="Data de referência (YYYY-MM-DD)"),
    data_inicio: Optional[date] = Query(None, description="Início do período (YYYY-MM-DD)"),
    data_fim: Optional[date] = Query(None, description="Fim do período (YYYY-MM-DD)"),
    modo: str = Query(
        "pendentes",
        description="pendentes = só abertos (web); saidas = todas as leituras do período por data operacional",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Quantidades por serviço do motoboy.
    - modo=pendentes: pendentes operacionais no período (compatível com a web).
    - modo=saidas: todas as saídas/leituras com data operacional no período
      (reatribuição/nova saída no dia D conta em D, não na Saida.data antiga).
      Cancelado/encerrado não entram no total (mesma regra da conferência).
    """
    sub_base = getattr(current_user, "sub_base", None)
    if not sub_base or not str(sub_base).strip():
        raise HTTPException(status_code=403, detail="Sub_base não definida.")

    inicio, fim = _resolver_periodo(data, data_inicio, data_fim)
    modo_norm = (modo or "pendentes").strip().lower()
    if modo_norm not in ("pendentes", "saidas"):
        raise HTTPException(status_code=400, detail="modo deve ser 'pendentes' ou 'saidas'.")

    if modo_norm == "saidas":
        rows_all = carregar_saidas_candidatas_periodo(
            db,
            sub_base=sub_base,
            motoboy_ids=[motoboy_id],
            inicio=inicio,
            fim=fim,
        )
        rows_periodo, _ = filtrar_saidas_por_periodo_operacional(db, rows_all, inicio, fim)
        rows_periodo = filtrar_status_conferencia(rows_periodo)
    else:
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
        rows_periodo = [
            s
            for s in rows_pendentes_validos
            if inicio
            <= (
                (
                    (ctx_map.get(s.id_saida).operacional_ts if ctx_map.get(s.id_saida) else None)
                    or s.timestamp
                ).date()
            )
            <= fim
        ]

    sum_shopee, sum_mercado, sum_avulso = _somar_servicos(rows_periodo)
    por_servico = _por_servico_breakdown(rows_periodo)
    pendentes_hoje = (
        por_servico.shopee.pendentes
        + por_servico.mercado_livre.pendentes
        + por_servico.avulso.pendentes
    )
    entregues_hoje = (
        por_servico.shopee.entregues
        + por_servico.mercado_livre.entregues
        + por_servico.avulso.entregues
    )
    ausentes_hoje = (
        por_servico.shopee.ausentes
        + por_servico.mercado_livre.ausentes
        + por_servico.avulso.ausentes
    )
    # modo=pendentes: mantém semântica antiga (todas as linhas são "pendentes").
    if modo_norm == "pendentes":
        pendentes_hoje = len(rows_periodo)

    motoboy_nome = _carregar_nomes_motoboy_ids(db, [motoboy_id]).get(
        motoboy_id, f"Motoboy {motoboy_id}"
    )

    return AcompanhamentoSaidasDiaResponse(
        data=fim.isoformat(),
        motoboy_id=motoboy_id,
        motoboy_nome=motoboy_nome,
        pendentes_hoje=pendentes_hoje,
        sum_shopee=sum_shopee,
        sum_mercado=sum_mercado,
        sum_avulso=sum_avulso,
        data_inicio=inicio.isoformat(),
        data_fim=fim.isoformat(),
        por_servico=por_servico,
        total_hoje=len(rows_periodo),
        entregues_hoje=entregues_hoje,
        ausentes_hoje=ausentes_hoje,
    )
