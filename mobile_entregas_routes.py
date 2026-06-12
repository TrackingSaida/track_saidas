"""
Rotas do App Motoboy (mobile).
Prefixo: /mobile
Requer JWT de motoboy (role=4, motoboy_id no token).
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Tuple, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, exists
from sqlalchemy.orm import Session

from db import get_db
from db_utils import db_rollback_safe
from auth import get_current_user
from active_route_sync import (
    get_active_route_delivery_ids,
    refresh_active_route_if_stale,
    sync_active_route_after_delivery_update,
)
from geocode_utils import (
    geocode_address_strict,
    haversine_m,
    otimizar_ordem_entregas,
    RoutePoint,
    StartPoint,
)
from models import (
    User,
    Saida,
    SaidaDetail,
    Motoboy,
    MotoboySubBase,
    MotivoAusencia,
    SaidaHistorico,
    RotasMotoboy,
    Owner,
    OwnerCobrancaItem,
)
from saidas_routes import (
    STATUS_SAIU_PARA_ENTREGA,
    STATUS_EM_ROTA,
    STATUS_ENTREGUE,
    STATUS_AUSENTE,
    STATUS_CANCELADO,
    _check_delete_window_or_409,
    _should_store_qr_payload_raw,
    normalizar_status_saida,
    _get_motoboy_nome,
    _status_esta_finalizado,
    _status_finalizado_detail,
)
from codigo_normalizer import (
    normalize_codigo,
    canonicalize_servico,
    is_qr_like_scan_payload,
)
from entregador_routes import resolver_precos_motoboy
from saida_operacional_utils import (
    carregar_contexto_operacional,
    EVENTOS_ATRIBUICAO_VALIDOS,
    deve_excluir_saida_operacional,
    timestamp_operacional_saida,
)
from log_leitura_service import registrar_log_leitura_critico
from pedido_campos_obrigatorios_service import (
    validate_campos_obrigatorios_conclusao,
    raise_if_campos_obrigatorios_faltando,
    format_bloqueio_motivo,
    resolve_campos_obrigatorios_ativos,
    build_campos_cache_for_sub_base,
    resolve_campos_obrigatorios_from_cache,
)

router = APIRouter(prefix="/mobile", tags=["Mobile - Entregas"])
OPERACAO_TZ = ZoneInfo("America/Sao_Paulo")


def _hoje_operacional() -> date:
    return datetime.now(OPERACAO_TZ).date()


# ============================================================
# Dep: usuário deve ser motoboy (role=4, motoboy_id no token)
# ============================================================
def get_current_motoboy(user: User = Depends(get_current_user)) -> User:
    if getattr(user, "role", 0) != 4:
        raise HTTPException(status_code=403, detail="Acesso restrito a motoboys.")
    if not getattr(user, "motoboy_id", None):
        raise HTTPException(status_code=403, detail="Token inválido para motoboy.")
    return user


def get_current_mobile_scan_user(user: User = Depends(get_current_user)) -> User:
    """Permite scan no mobile para motoboy e staff (admin/operação)."""
    role = int(getattr(user, "role", 0) or 0)
    if role not in (0, 1, 2, 3, 4):
        raise HTTPException(status_code=403, detail="Perfil sem acesso ao scan mobile.")
    if role == 4 and not getattr(user, "motoboy_id", None):
        raise HTTPException(status_code=403, detail="Token inválido para motoboy.")
    return user


# ============================================================
# Schemas
# ============================================================
class EntregaListItem(BaseModel):
    id_saida: int
    codigo: Optional[str]
    status: str
    exibicao: str  # "Pendente" | "Entregue" | "Ausente"
    servico: Optional[str] = None  # Shopee | Mercado Livre | Flex | Avulso
    cliente: Optional[str] = None
    bairro: Optional[str] = None
    endereco: Optional[str] = None
    numero: Optional[str] = None  # dest_numero (para agrupamento CEP+número)
    cep: Optional[str] = None  # dest_cep (para agrupamento CEP+número)
    cidade: Optional[str] = None
    estado: Optional[str] = None
    contato: Optional[str] = None
    data: Optional[date] = None
    data_hora_entrega: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    endereco_formatado: Optional[str] = None
    endereco_origem: Optional[str] = None  # manual | ocr | voz | suggestion | autocomplete | mapa | google_places
    coord_precision: Optional[str] = None  # rooftop | street | approx
    geocode_source: Optional[str] = None
    geocode_score: Optional[float] = None
    geocoded_at: Optional[datetime] = None
    possui_endereco: bool = False
    tentativa: Optional[int] = None  # 1 = primeira; >= 2 exibe "Xª tentativa"
    tem_comprovante: bool = False
    tipo_recebedor: Optional[str] = None
    nome_recebedor: Optional[str] = None
    tipo_documento: Optional[str] = None
    numero_documento: Optional[str] = None
    observacao_entrega: Optional[str] = None
    observacao_ocorrencia: Optional[str] = None
    motivo_ocorrencia: Optional[str] = None
    complemento: Optional[str] = None
    data_hora_ocorrencia: Optional[datetime] = None
    campos_obrigatorios: List[str] = Field(default_factory=list)
    campos_obrigatorios_entregue: List[str] = Field(default_factory=list)
    campos_obrigatorios_ausente: List[str] = Field(default_factory=list)


class ScanBody(BaseModel):
    codigo: str = Field(min_length=1)
    origem: str = "camera"  # camera | manual


class ConfirmarNovaSaidaMesmoEntregadorBody(BaseModel):
    origem: str = "mobile"


class AusenteBody(BaseModel):
    motivo_id: int
    observacao: Optional[str] = None


class EntregueBody(BaseModel):
    tipo_recebedor: Optional[str] = None
    nome_recebedor: Optional[str] = None
    tipo_documento: Optional[str] = None
    numero_documento: Optional[str] = None
    observacao_entrega: Optional[str] = None


class FinalizarLoteBody(BaseModel):
    ids: List[int] = Field(min_length=1, max_length=50)
    acao: Literal["entregue", "ausente"]
    motivo_id: Optional[int] = None
    observacao: Optional[str] = None


class FinalizarLoteItemOut(BaseModel):
    id_saida: int
    status: str


class FinalizarLoteBloqueadoOut(BaseModel):
    id_saida: int
    codigo: Optional[str] = None
    motivo: str


class FinalizarLoteErroOut(BaseModel):
    id_saida: int
    mensagem: str


class RotaSyncOut(BaseModel):
    in_active_route: bool = False
    rota_finalizada: bool = False
    rota_id: Optional[str] = None
    parada_atual: Optional[int] = None
    ordem: Optional[List[int]] = None


class FinalizarLoteResponse(BaseModel):
    finalizados: List[FinalizarLoteItemOut]
    bloqueados: List[FinalizarLoteBloqueadoOut]
    erros: List[FinalizarLoteErroOut]
    rota_sync: Optional[RotaSyncOut] = None


class EnderecoBody(BaseModel):
    destinatario: str = Field(min_length=1)
    rua: str = Field(min_length=1)
    numero: str = Field(min_length=1)
    complemento: Optional[str] = None
    bairro: str = Field(min_length=1)
    cidade: str = Field(min_length=1)
    estado: str = Field(min_length=1)
    cep: str = Field(min_length=8)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    origem: str = "manual"  # manual | ocr | voz | suggestion | autocomplete | mapa | google_places
    coord_precision: Optional[str] = None  # rooftop | street | approx
    geocode_source: Optional[str] = None
    geocode_score: Optional[float] = None


class EnderecoSugestoesHints(BaseModel):
    rua: Optional[str] = None
    numero: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None
    cep: Optional[str] = None


class EnderecoSugestoesBody(BaseModel):
    query: str = Field(min_length=3)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    hints: Optional[EnderecoSugestoesHints] = None
    limit: int = Field(default=8, ge=1, le=15)
    session_token: Optional[str] = None
    allow_google_fallback: bool = False
    google_fallback_reason: Optional[str] = None  # user_requested | timeout | auto | no_results


class EnderecoSugestaoOut(BaseModel):
    label: str
    rua: str
    numero: str = ""
    bairro: str = ""
    cidade: str = ""
    estado: str = ""
    cep: str = ""
    latitude: float
    longitude: float
    score: int
    confidence: float = 0.0
    source: str
    distance_km: Optional[float] = None
    distance_meters: Optional[int] = None
    badge: Optional[str] = None
    already_used: bool = False
    main_text: Optional[str] = None
    secondary_text: Optional[str] = None
    place_id: Optional[str] = None
    requires_place_details: bool = False


class EnderecoSugestoesResponse(BaseModel):
    suggestions: List[EnderecoSugestaoOut]
    did_you_mean: Optional[dict] = None
    used_google: bool = False


class PlaceDetailsBody(BaseModel):
    place_id: str = Field(min_length=1)
    session_token: Optional[str] = None
    query: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    hints: Optional[EnderecoSugestoesHints] = None


class PlaceDetailsResponse(BaseModel):
    suggestion: Optional[EnderecoSugestaoOut] = None


class IniciarRotaBody(BaseModel):
    delivery_ids: Optional[List[int]] = None  # se enviado, só esses id_saida vão para EM_ROTA


class MotivoAusenciaOut(BaseModel):
    id: int
    descricao: str


class RoutePointBody(BaseModel):
    latitude: float
    longitude: float


class RotasPriorityBody(BaseModel):
    type: str  # service | delivery
    value: Optional[str] = None  # Shopee | Flex | Avulso
    id_saida: Optional[int] = None


class RotasOtimizarBody(BaseModel):
    delivery_ids: List[int] = Field(..., min_length=1, max_length=100)
    start: Optional[RoutePointBody] = None
    priority: Optional[RotasPriorityBody] = None


class RotasOtimizarOut(BaseModel):
    ordem: List[int]
    modo: str
    sem_coordenadas: List[int]
    distancia_total_m: Optional[int] = None
    duracao_total_s: Optional[int] = None


class RotasIniciarBody(BaseModel):
    ordem: List[int] = Field(..., min_length=1)


class RotasIniciarOut(BaseModel):
    rota_id: str


class RotasAvancarOut(BaseModel):
    parada_atual: int


class RotasAtivaOut(BaseModel):
    rota_id: str
    ordem: List[int]
    parada_atual: int
    data: Optional[str] = None


class RotasOrdemBody(BaseModel):
    ordem: List[int] = Field(..., min_length=1)


class RotasOrdemOut(BaseModel):
    ordem: List[int]
    parada_atual: int


class RotasResumoOut(BaseModel):
    rota_id: int
    paradas: int
    pedidos: int
    entregues: int
    ausentes: int
    pendentes: int
    valor_total: Decimal


class ExtratoDiaItem(BaseModel):
    data: str
    total_pacotes_associados: int
    total_pacotes_filtrados: int
    valor_dia: Decimal
    itens: List["ExtratoPedidoItem"]


class ExtratoPedidoItem(BaseModel):
    id_saida: int
    codigo: Optional[str]
    status: str
    exibicao: str
    servico: str


class ExtratoFinanceiroOut(BaseModel):
    periodo_inicio: str
    periodo_fim: str
    status_filtro: str
    valor_a_receber: Decimal
    total_pacotes_associados: int
    total_pacotes_filtrados: int
    total_cancelados: int
    resumo_por_servico: Dict[str, int]
    dias: List[ExtratoDiaItem]


# ============================================================
# Helpers
# ============================================================
def _status_exibicao(status: Optional[str]) -> str:
    if not status:
        return "Pendente"
    s = (status or "").strip().upper()
    if s in (STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA):
        return "Pendente"
    if s == STATUS_ENTREGUE:
        return "Entregue"
    if s == STATUS_AUSENTE:
        return "Ausente"
    if s == STATUS_CANCELADO:
        return "Cancelado"
    return status or "Pendente"


def _get_saida_for_motoboy(db: Session, id_saida: int, motoboy_id: int, sub_base: str) -> Saida:
    obj = db.get(Saida, id_saida)
    if not obj or obj.sub_base != sub_base or obj.motoboy_id != motoboy_id:
        raise HTTPException(status_code=404, detail="Entrega não encontrada.")
    return obj


def _get_detail_for_saida(db: Session, id_saida: int) -> Optional[SaidaDetail]:
    return db.scalar(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida == id_saida)
        .order_by(SaidaDetail.id_detail.desc())
        .limit(1)
    )


def _carregar_details_por_saida_ids(db: Session, saida_ids: List[int]) -> Dict[int, SaidaDetail]:
    ids = sorted({int(i) for i in saida_ids if i is not None})
    if not ids:
        return {}
    latest_ids_subq = (
        select(
            SaidaDetail.id_saida.label("id_saida"),
            func.max(SaidaDetail.id_detail).label("max_id_detail"),
        )
        .where(SaidaDetail.id_saida.in_(ids))
        .group_by(SaidaDetail.id_saida)
        .subquery()
    )
    rows = db.execute(
        select(SaidaDetail)
        .join(latest_ids_subq, SaidaDetail.id_detail == latest_ids_subq.c.max_id_detail)
        .order_by(SaidaDetail.id_saida.asc())
    ).scalars().all()
    out: Dict[int, SaidaDetail] = {}
    for d in rows:
        sid = int(d.id_saida)
        if sid not in out:
            out[sid] = d
    return out


def _servico_tipo(serv: Optional[str]) -> str:
    """Retorna Shopee | Flex | Avulso para exibição."""
    s = (serv or "").strip().lower()
    if "shopee" in s:
        return "Shopee"
    if "mercado" in s or "ml" in s or "flex" in s:
        return "Flex"
    return "Avulso"


def _possui_endereco(detail: Optional[SaidaDetail]) -> bool:
    if not detail:
        return False
    if detail.endereco_formatado and detail.endereco_formatado.strip():
        return True
    return bool((detail.dest_rua or "").strip() and (detail.dest_numero or "").strip())


def _carregar_data_hora_ocorrencia_map(db: Session, ids: List[int]) -> Dict[int, datetime]:
    if not ids:
        return {}
    rows = db.execute(
        select(SaidaHistorico.id_saida, func.max(SaidaHistorico.timestamp)).where(
            SaidaHistorico.id_saida.in_(ids),
            SaidaHistorico.evento.in_(("ausente", "entregue", "cancelado")),
        ).group_by(SaidaHistorico.id_saida)
    ).all()
    return {int(r[0]): r[1] for r in rows}


def _saida_to_item(
    s: Saida,
    detail: Optional[SaidaDetail],
    *,
    data_hora_ocorrencia: Optional[datetime] = None,
) -> dict:
    endereco = None
    if detail and (detail.dest_rua or detail.dest_numero):
        parts = [p for p in [detail.dest_rua, detail.dest_numero, detail.dest_complemento] if p]
        endereco = ", ".join(parts) if parts else None
    lat = float(detail.latitude) if detail and detail.latitude is not None else None
    lon = float(detail.longitude) if detail and detail.longitude is not None else None
    tem_comprovante = bool((detail.foto_url or "").strip()) if detail else False
    return {
        "id_saida": s.id_saida,
        "codigo": s.codigo,
        "status": s.status or "",
        "exibicao": _status_exibicao(s.status),
        "servico": s.servico,
        "cliente": detail.dest_nome if detail else None,
        "bairro": detail.dest_bairro if detail else None,
        "endereco": endereco,
        "numero": (detail.dest_numero or "").strip() or None if detail else None,
        "cep": (detail.dest_cep or "").strip() or None if detail else None,
        "cidade": (detail.dest_cidade or "").strip() or None if detail else None,
        "estado": (detail.dest_estado or "").strip() or None if detail else None,
        "contato": detail.dest_contato if detail else None,
        "data": s.data,
        "data_hora_entrega": s.data_hora_entrega,
        "latitude": lat,
        "longitude": lon,
        "endereco_formatado": (detail.endereco_formatado or "").strip() or None if detail else None,
        "endereco_origem": (detail.endereco_origem or "").strip() or None if detail else None,
        "coord_precision": (detail.coord_precision or "").strip() or None if detail else None,
        "geocode_source": (getattr(detail, "geocode_source", None) or "").strip() or None if detail else None,
        "geocode_score": float(detail.geocode_score) if detail and detail.geocode_score is not None else None,
        "geocoded_at": detail.geocoded_at if detail and getattr(detail, "geocoded_at", None) else None,
        "possui_endereco": _possui_endereco(detail),
        "tentativa": (detail.tentativa if detail and getattr(detail, "tentativa", None) is not None else None) or 1,
        "tem_comprovante": tem_comprovante,
        "tipo_recebedor": (detail.tipo_recebedor or "").strip() or None if detail else None,
        "nome_recebedor": (detail.nome_recebedor or "").strip() or None if detail else None,
        "tipo_documento": (detail.tipo_documento or "").strip() or None if detail else None,
        "numero_documento": (detail.numero_documento or "").strip() or None if detail else None,
        "observacao_entrega": (detail.observacao_entrega or "").strip() or None if detail else None,
        "observacao_ocorrencia": (detail.observacao_ocorrencia or "").strip() or None if detail else None,
        "motivo_ocorrencia": (detail.motivo_ocorrencia or "").strip() or None if detail else None,
        "complemento": (detail.dest_complemento or "").strip() or None if detail else None,
        "data_hora_ocorrencia": data_hora_ocorrencia,
        "campos_obrigatorios": [],
        "campos_obrigatorios_entregue": [],
        "campos_obrigatorios_ausente": [],
    }


def _parse_data_yyyy_mm_dd(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _periodo_quinzena_atual(ref: date) -> Tuple[date, date]:
    if ref.day <= 15:
        return date(ref.year, ref.month, 1), ref
    return date(ref.year, ref.month, 16), ref


def _status_normalizado_upper(status: Optional[str]) -> str:
    if not status:
        return ""
    return normalizar_status_saida(status).strip().upper()


def _valor_saida(precos: Dict[str, Decimal], saida: Saida) -> Decimal:
    t = _servico_tipo(saida.servico)
    if t == "Shopee":
        return precos["shopee"]
    if t == "Flex":
        return precos["flex"]
    return precos["avulso"]


def _precos_mobile_map(db: Session, sub_base: str, motoboy_id: int) -> Dict[str, Decimal]:
    precos = resolver_precos_motoboy(db, sub_base, motoboy_id=motoboy_id)
    return {
        "shopee": precos["shopee_valor"],
        "flex": precos["ml_valor"],
        "avulso": precos["avulso_valor"],
    }


def _valor_finalizado_hoje_motoboy(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    hoje: date,
) -> Decimal:
    """Soma remuneração de pedidos com evento entregue/ausente na data de conclusão (calendário)."""
    ids_hoje = set(
        db.scalars(
            select(SaidaHistorico.id_saida).where(
                SaidaHistorico.evento.in_(("entregue", "ausente")),
                func.date(SaidaHistorico.timestamp) == hoje,
            )
        ).all()
    )
    if not ids_hoje:
        return Decimal("0.00")
    rows = db.scalars(
        select(Saida).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.codigo.isnot(None),
            Saida.id_saida.in_(ids_hoje),
            Saida.status.in_([STATUS_ENTREGUE, STATUS_AUSENTE]),
        )
    ).all()
    precos = _precos_mobile_map(db, sub_base, motoboy_id)
    total = Decimal("0.00")
    for s in rows:
        total += _valor_saida(precos, s)
    return total.quantize(Decimal("0.01"))


def _resumo_rota_motoboy(
    db: Session,
    *,
    rota: RotasMotoboy,
    sub_base: str,
    motoboy_id: int,
) -> RotasResumoOut:
    ordem_raw = json.loads(rota.ordem_json) if isinstance(rota.ordem_json, str) else rota.ordem_json
    ids = [int(x) for x in (ordem_raw or [])]
    if not ids:
        return RotasResumoOut(
            rota_id=int(rota.id),
            paradas=0,
            pedidos=0,
            entregues=0,
            ausentes=0,
            pendentes=0,
            valor_total=Decimal("0.00"),
        )
    rows = db.scalars(
        select(Saida).where(
            Saida.id_saida.in_(ids),
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
        )
    ).all()
    rows_by_id = {int(s.id_saida): s for s in rows}
    details_map = _carregar_details_por_saida_ids(db, ids)
    from route_stops import build_route_stops

    stops = build_route_stops(ids, details_map)
    entregues = 0
    ausentes = 0
    pendentes = 0
    precos = _precos_mobile_map(db, sub_base, motoboy_id)
    valor_total = Decimal("0.00")
    for id_saida in ids:
        s = rows_by_id.get(id_saida)
        if not s:
            pendentes += 1
            continue
        status_up = _status_normalizado_upper(s.status)
        if status_up == STATUS_ENTREGUE:
            entregues += 1
            valor_total += _valor_saida(precos, s)
        elif status_up == STATUS_AUSENTE:
            ausentes += 1
            valor_total += _valor_saida(precos, s)
        elif status_up in (STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA):
            pendentes += 1
        else:
            pendentes += 1
    return RotasResumoOut(
        rota_id=int(rota.id),
        paradas=len(stops),
        pedidos=len(ids),
        entregues=entregues,
        ausentes=ausentes,
        pendentes=pendentes,
        valor_total=valor_total.quantize(Decimal("0.01")),
    )


def _marcacao_response(
    db: Session,
    saida: Saida,
    *,
    complemento: bool = False,
    motoboy_id: Optional[int] = None,
) -> dict:
    hoje = _hoje_operacional()
    data_op = _ctx_data_operacional_saida(db, saida)
    payload = {
        "ok": True,
        "id_saida": int(saida.id_saida),
        "entrega_atrasada": data_op < hoje,
        "data_operacional": data_op.isoformat(),
    }
    if complemento:
        payload["complemento"] = True
    if motoboy_id is not None:
        sync = sync_active_route_after_delivery_update(db, motoboy_id, int(saida.id_saida))
        payload["rota_sync"] = sync
    return payload


def _filtrar_por_data_operacional(
    db: Session,
    saidas: List[Saida],
    data_ref: Optional[date],
) -> List[Saida]:
    if not saidas or data_ref is None:
        return list(saidas)
    ctx_map = carregar_contexto_operacional(db, [s.id_saida for s in saidas])
    out: List[Saida] = []
    for s in saidas:
        ctx = ctx_map.get(s.id_saida)
        if deve_excluir_saida_operacional(ctx):
            continue
        ts = timestamp_operacional_saida(ctx, s.timestamp)
        if ts and ts.date() == data_ref:
            out.append(s)
    return out


def _ctx_data_operacional_saida(db: Session, saida: Saida) -> date:
    ctx_map = carregar_contexto_operacional(db, [saida.id_saida])
    ctx = ctx_map.get(saida.id_saida)
    ts_op = (ctx.operacional_ts if ctx and ctx.operacional_ts else None) or saida.timestamp
    return ts_op.date() if ts_op else (saida.data or _hoje_operacional())


def _payload_nova_saida_mesmo_entregador(
    *,
    data_operacional_anterior: date,
    data_operacional_nova: date,
    id_motoboy: Optional[int],
    confirmado_por: Optional[str],
    origem: str,
) -> str:
    return json.dumps(
        {
            "tipo_evento": "nova_saida_mesmo_entregador",
            "data_operacional_anterior": data_operacional_anterior.isoformat(),
            "data_operacional_nova": data_operacional_nova.isoformat(),
            "id_motoboy": int(id_motoboy) if id_motoboy is not None else None,
            "confirmado_por": confirmado_por or "",
            "origem": origem or "mobile",
            "data_hora_confirmacao": datetime.now().isoformat(sep=" ", timespec="seconds"),
        },
        ensure_ascii=False,
    )


# ============================================================
# GET /mobile/entregas
# ============================================================
@router.get("/entregas", response_model=List[EntregaListItem])
def listar_entregas(
    status: Optional[str] = None,
    dia: Optional[str] = None,
    data: Optional[str] = None,
    subtipo: Optional[str] = Query(
        None,
        description="Para status=finalizadas: entregue (padrão) ou cancelado",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Lista entregas do motoboy. status=pendente | finalizadas | ausentes.
    dia=hoje + data (YYYY-MM-DD): filtra finalizadas por data_hora_entrega e ausentes por data.
    Sem data, usa date.today() do servidor.
    """
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")
    # Filtro por data: quando dia=hoje (ou data enviada) para pendentes/finalizadas/ausentes
    usar_filtro_hoje = (dia == "hoje") or (status in ("pendente", "finalizadas", "ausentes") and data)
    if usar_filtro_hoje:
        if data:
            try:
                hoje = date.fromisoformat(data.strip())
            except ValueError:
                hoje = _hoje_operacional()
        else:
            hoje = _hoje_operacional()
    else:
        hoje = None

    q = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.motoboy_id == motoboy_id,
        Saida.codigo.isnot(None),
    )
    if status == "pendente":
        q = q.where(Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]))
    elif status == "finalizadas":
        subtipo_norm = (subtipo or "entregue").strip().lower()
        if subtipo_norm == "cancelado":
            q = q.where(Saida.status == STATUS_CANCELADO)
        else:
            q = q.where(Saida.status == STATUS_ENTREGUE)
        if hoje is not None:
            if subtipo_norm == "cancelado":
                subq_cancel = select(1).where(
                    SaidaHistorico.id_saida == Saida.id_saida,
                    SaidaHistorico.evento == "cancelado",
                    func.date(SaidaHistorico.timestamp) == hoje,
                )
                q = q.where(exists(subq_cancel))
            else:
                subq_entregue = select(1).where(
                    SaidaHistorico.id_saida == Saida.id_saida,
                    SaidaHistorico.evento == "entregue",
                    func.date(SaidaHistorico.timestamp) == hoje,
                )
                q = q.where(exists(subq_entregue))
    elif status == "ausentes":
        q = q.where(Saida.status == STATUS_AUSENTE)
        if hoje is not None:
            # Filtro "ausentes hoje": pelo evento ausente no histórico com data = hoje
            subq_ausente = select(1).where(
                SaidaHistorico.id_saida == Saida.id_saida,
                SaidaHistorico.evento == "ausente",
                func.date(SaidaHistorico.timestamp) == hoje,
            )
            q = q.where(exists(subq_ausente))
    q = q.order_by(Saida.data.desc(), Saida.timestamp.desc())

    rows = db.scalars(q).all()
    if status == "pendente" and hoje is not None:
        rows = _filtrar_por_data_operacional(db, rows, hoje)
    ids = [int(s.id_saida) for s in rows]
    details_map = _carregar_details_por_saida_ids(db, ids)
    ocorrencia_map = _carregar_data_hora_ocorrencia_map(db, ids)
    campos_cache = build_campos_cache_for_sub_base(db, sub_base=sub_base)
    out = []
    for s in rows:
        sid = int(s.id_saida)
        item = _saida_to_item(
            s,
            details_map.get(sid),
            data_hora_ocorrencia=ocorrencia_map.get(sid),
        )
        campos_entregue = resolve_campos_obrigatorios_from_cache(
            cache=campos_cache,
            servico=s.servico,
            contexto="ENTREGUE",
        )
        campos_ausente = resolve_campos_obrigatorios_from_cache(
            cache=campos_cache,
            servico=s.servico,
            contexto="AUSENTE",
        )
        item["campos_obrigatorios"] = sorted(set((campos_entregue or []) + (campos_ausente or [])))
        item["campos_obrigatorios_entregue"] = campos_entregue or []
        item["campos_obrigatorios_ausente"] = campos_ausente or []
        out.append(item)
    return out


@router.get("/entregas/extrato", response_model=ExtratoFinanceiroOut)
def extrato_financeiro_motoboy(
    data_inicio: Optional[str] = Query(None, description="YYYY-MM-DD"),
    data_fim: Optional[str] = Query(None, description="YYYY-MM-DD"),
    status_filtro: str = Query("grupo_entregue", description="grupo_entregue | todos | cancelados"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    hoje = _hoje_operacional()
    inicio_in = _parse_data_yyyy_mm_dd(data_inicio)
    fim_in = _parse_data_yyyy_mm_dd(data_fim)
    if inicio_in is None or fim_in is None:
        periodo_inicio, periodo_fim = _periodo_quinzena_atual(hoje)
    else:
        periodo_inicio, periodo_fim = inicio_in, fim_in
    if periodo_inicio > periodo_fim:
        raise HTTPException(status_code=400, detail="data_inicio deve ser menor ou igual a data_fim.")

    modo = (status_filtro or "grupo_entregue").strip().lower()
    if modo not in ("grupo_entregue", "todos", "cancelados"):
        modo = "grupo_entregue"

    dt_inicio = datetime.combine(periodo_inicio, datetime.min.time())
    dt_fim_exclusivo = datetime.combine(periodo_fim + timedelta(days=1), datetime.min.time())
    eventos_operacionais = tuple(EVENTOS_ATRIBUICAO_VALIDOS)
    subq_hist_periodo = select(1).where(
        SaidaHistorico.id_saida == Saida.id_saida,
        SaidaHistorico.evento.in_(eventos_operacionais),
        SaidaHistorico.timestamp >= dt_inicio,
        SaidaHistorico.timestamp < dt_fim_exclusivo,
    )
    q = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.motoboy_id == motoboy_id,
        Saida.codigo.isnot(None),
        (
            (Saida.timestamp >= dt_inicio)
            & (Saida.timestamp < dt_fim_exclusivo)
        )
        | exists(subq_hist_periodo),
    ).order_by(Saida.data.desc(), Saida.timestamp.desc())
    rows_all = db.scalars(q).all()
    rows_periodo = list(rows_all)
    op_ctx_map = carregar_contexto_operacional(db, [s.id_saida for s in rows_periodo])
    rows: List[Saida] = []
    for s in rows_periodo:
        ctx = op_ctx_map.get(s.id_saida)
        if deve_excluir_saida_operacional(ctx):
            continue
        ts_op = timestamp_operacional_saida(ctx, s.timestamp)
        if ts_op is None:
            continue
        if ts_op.date() < periodo_inicio or ts_op.date() > periodo_fim:
            continue
        rows.append(s)

    grupo_entregue = {STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA, STATUS_ENTREGUE}
    precos = resolver_precos_motoboy(db, sub_base, motoboy_id=motoboy_id)
    precos_mobile = {
        "shopee": precos["shopee_valor"],
        "flex": precos["ml_valor"],
        "avulso": precos["avulso_valor"],
    }
    valor_total = Decimal("0.00")
    total_associados = 0
    total_filtrados = 0
    total_cancelados = 0
    por_servico = {"Shopee": 0, "Flex": 0, "Avulso": 0}
    dias_map: Dict[str, Dict[str, Decimal | int | List[ExtratoPedidoItem]]] = {}

    for s in rows:
        status_up = _status_normalizado_upper(s.status)
        is_cancelado = status_up == STATUS_CANCELADO
        is_grupo_entregue = status_up in grupo_entregue
        if modo == "cancelados":
            passa_filtro = is_cancelado
        elif modo == "todos":
            passa_filtro = True
        else:
            passa_filtro = is_grupo_entregue

        ctx = op_ctx_map.get(s.id_saida)
        ts_op = (ctx.operacional_ts if ctx and ctx.operacional_ts else None) or s.timestamp
        d = ts_op.date().isoformat() if ts_op else ""
        if d and d not in dias_map:
            dias_map[d] = {
                "total_pacotes_associados": 0,
                "total_pacotes_filtrados": 0,
                "valor_dia": Decimal("0.00"),
                "itens": [],
            }

        if is_cancelado:
            total_cancelados += 1
        else:
            total_associados += 1
            if d:
                dias_map[d]["total_pacotes_associados"] += 1

        if not passa_filtro:
            continue
        total_filtrados += 1
        if d:
            dias_map[d]["total_pacotes_filtrados"] += 1
            dias_map[d]["itens"].append(
                ExtratoPedidoItem(
                    id_saida=s.id_saida,
                    codigo=s.codigo,
                    status=s.status or "",
                    exibicao=_status_exibicao(s.status),
                    servico=_servico_tipo(s.servico),
                )
            )

        tipo = _servico_tipo(s.servico)
        if tipo in por_servico:
            por_servico[tipo] += 1

        if is_cancelado:
            continue
        valor = _valor_saida(precos_mobile, s)
        valor_total += valor
        if d:
            dias_map[d]["valor_dia"] += valor

    dias = []
    for d, v in dias_map.items():
        if int(v["total_pacotes_filtrados"]) <= 0:
            continue
        dias.append(
            ExtratoDiaItem(
                data=d,
                total_pacotes_associados=int(v["total_pacotes_associados"]),
                total_pacotes_filtrados=int(v["total_pacotes_filtrados"]),
                valor_dia=Decimal(v["valor_dia"]).quantize(Decimal("0.01")),
                itens=list(v["itens"]),
            )
        )
    dias.sort(key=lambda item: item.data, reverse=True)

    return ExtratoFinanceiroOut(
        periodo_inicio=periodo_inicio.isoformat(),
        periodo_fim=periodo_fim.isoformat(),
        status_filtro=modo,
        valor_a_receber=valor_total.quantize(Decimal("0.01")),
        total_pacotes_associados=total_associados,
        total_pacotes_filtrados=total_filtrados,
        total_cancelados=total_cancelados,
        resumo_por_servico={
            "shopee": por_servico["Shopee"],
            "flex": por_servico["Flex"],
            "avulso": por_servico["Avulso"],
        },
        dias=dias,
    )


# ============================================================
# GET /mobile/entregas/resumo
# ============================================================
@router.get("/entregas/resumo")
def resumo_entregas(
    data: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Contadores: pendentes, finalizadas_hoje, ausentes, atraso_d1.
    data (opcional, YYYY-MM-DD): data local do app para 'hoje'; finalizadas_hoje e atraso_d1 usam essa data.
    Sem data, usa date.today() do servidor.
    """
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    if data:
        try:
            hoje = date.fromisoformat(data.strip())
        except ValueError:
            hoje = _hoje_operacional()
    else:
        hoje = _hoje_operacional()
    rows_pendentes_all = db.scalars(
        select(Saida).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.codigo.isnot(None),
            Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]),
        )
    ).all()
    ctx_map_pendentes = carregar_contexto_operacional(db, [s.id_saida for s in rows_pendentes_all])
    rows_pendentes = [
        s
        for s in rows_pendentes_all
        if not deve_excluir_saida_operacional(ctx_map_pendentes.get(s.id_saida))
    ]
    rows_pendentes_hoje = _filtrar_por_data_operacional(db, rows_pendentes, hoje)
    pendentes = len(rows_pendentes_hoje)
    # Finalizadas hoje: baseia-se no evento "entregue" do histórico para alinhar com as telas de registros.
    finalizadas_hoje = db.scalar(
        select(func.count(Saida.id_saida))
        .where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.codigo.isnot(None),
            Saida.status == STATUS_ENTREGUE,
            exists(
                select(1).where(
                    SaidaHistorico.id_saida == Saida.id_saida,
                    SaidaHistorico.evento == "entregue",
                    func.date(SaidaHistorico.timestamp) == hoje,
                )
            ),
        )
    ) or 0
    tem_saiu_para_entrega = db.scalar(
        select(func.count(Saida.id_saida)).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.codigo.isnot(None),
            Saida.status == STATUS_SAIU_PARA_ENTREGA,
        )
    ) or 0
    ausentes = db.scalar(
        select(func.count(Saida.id_saida)).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.codigo.isnot(None),
            Saida.status == STATUS_AUSENTE,
        )
    ) or 0
    ausentes_hoje = db.scalar(
        select(func.count(Saida.id_saida)).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.codigo.isnot(None),
            Saida.status == STATUS_AUSENTE,
            exists(
                select(1).where(
                    SaidaHistorico.id_saida == Saida.id_saida,
                    SaidaHistorico.evento == "ausente",
                    func.date(SaidaHistorico.timestamp) == hoje,
                )
            ),
        )
    ) or 0
    total_finalizado_hoje = int(finalizadas_hoje) + int(ausentes_hoje)
    # Em atraso (D+1): considera data operacional da última ação válida.
    atraso_d1 = sum(
        1
        for s in rows_pendentes
        if (
            (ts := timestamp_operacional_saida(ctx_map_pendentes.get(s.id_saida), s.timestamp))
            and ts.date() < hoje
        )
    )

    valor_finalizado_hoje = _valor_finalizado_hoje_motoboy(
        db, sub_base=sub_base, motoboy_id=motoboy_id, hoje=hoje
    )

    return {
        "pendentes": pendentes,
        "finalizadas_hoje": finalizadas_hoje,
        "ausentes_hoje": ausentes_hoje,
        "total_finalizado_hoje": total_finalizado_hoje,
        "pode_iniciar_rota": tem_saiu_para_entrega > 0,
        "ausentes": ausentes,
        "atraso_d1": atraso_d1,
        "valor_finalizado_hoje": valor_finalizado_hoje,
    }


# ============================================================
# POST /mobile/iniciar-rota
# ============================================================
@router.post("/iniciar-rota")
def iniciar_rota(
    body: Optional[IniciarRotaBody] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Atualiza SAIU_PARA_ENTREGA para EM_ROTA. Se body.delivery_ids for enviado, só esses id_saida; senão, todas do motoboy."""
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    if body and body.delivery_ids:
        ids = body.delivery_ids
        result = db.execute(
            select(Saida).where(
                Saida.id_saida.in_(ids),
                Saida.sub_base == sub_base,
                Saida.motoboy_id == motoboy_id,
                Saida.status == STATUS_SAIU_PARA_ENTREGA,
            )
        )
        rows = result.scalars().all()
    else:
        result = db.execute(
            select(Saida).where(
                Saida.sub_base == sub_base,
                Saida.motoboy_id == motoboy_id,
                Saida.status == STATUS_SAIU_PARA_ENTREGA,
            )
        )
        rows = result.scalars().all()
    for s in rows:
        s.status = STATUS_EM_ROTA
    for s in rows:
        db.add(
            SaidaHistorico(
                id_saida=s.id_saida,
                evento="em_rota",
                status_anterior=STATUS_SAIU_PARA_ENTREGA,
                status_novo=STATUS_EM_ROTA,
                user_id=user.id,
            )
        )
    db.commit()
    return {"atualizados": len(rows)}


# ============================================================
# POST /mobile/rotas/otimizar
# ============================================================
@router.post("/rotas/otimizar", response_model=RotasOtimizarOut)
def rotas_otimizar(
    body: RotasOtimizarBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Otimiza ordem das entregas (OSRM Trip com fallback nearest neighbor)."""
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    delivery_ids = list(dict.fromkeys(int(i) for i in body.delivery_ids))
    if len(delivery_ids) > 100:
        raise HTTPException(status_code=400, detail="Máximo de 100 entregas por otimização.")

    rows = db.scalars(
        select(Saida).where(
            Saida.id_saida.in_(delivery_ids),
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]),
        )
    ).all()
    found_ids = {int(s.id_saida) for s in rows}
    if len(found_ids) != len(delivery_ids):
        raise HTTPException(
            status_code=422,
            detail="Um ou mais pedidos são inválidos ou não pertencem ao motoboy.",
        )
    if not rows:
        raise HTTPException(status_code=400, detail="Nenhuma entrega válida para otimização.")

    from route_stops import build_route_stops, expand_stop_order

    details_map = _carregar_details_por_saida_ids(db, delivery_ids)
    stops = build_route_stops(delivery_ids, details_map)

    com_coord: List[RoutePoint] = []
    sem_coordenadas: List[int] = []
    stops_with_coords = 0
    stops_without_coords = 0

    for stop in stops:
        if stop.has_coords:
            stops_with_coords += 1
            com_coord.append((stop.representative_id, stop.lat, stop.lon))
        else:
            stops_without_coords += 1
            sem_coordenadas.extend(stop.delivery_ids)

    logging.getLogger(__name__).info(
        "rotas_otimizar: %s entregas → %s paradas (%s com coords, %s sem), %s entregas sem coordenadas",
        len(delivery_ids),
        len(stops),
        stops_with_coords,
        stops_without_coords,
        len(sem_coordenadas),
    )

    start: Optional[StartPoint] = None
    if body.start is not None:
        start = (float(body.start.latitude), float(body.start.longitude))

    if not com_coord:
        return RotasOtimizarOut(
            ordem=list(sem_coordenadas),
            modo="nearest_fallback",
            sem_coordenadas=sem_coordenadas,
            distancia_total_m=None,
            duracao_total_s=None,
        )

    stop_penalties = None
    if body.priority is not None:
        from geocode_utils import SOFT_PRIORITY_PENALTY_M

        saida_by_id = {int(s.id_saida): s for s in rows}
        stop_penalties = {}
        prio = body.priority
        if prio.type == "service" and prio.value:
            target = str(prio.value).strip()
            if target not in ("Shopee", "Flex", "Avulso"):
                raise HTTPException(status_code=422, detail="Serviço de prioridade inválido.")
            for stop in stops:
                if not stop.has_coords:
                    continue
                matched = any(
                    _servico_tipo(saida_by_id[d].servico) == target
                    for d in stop.delivery_ids
                    if d in saida_by_id
                )
                stop_penalties[stop.representative_id] = 0.0 if matched else SOFT_PRIORITY_PENALTY_M
        elif prio.type == "delivery" and prio.id_saida is not None:
            target_id = int(prio.id_saida)
            if target_id not in found_ids:
                raise HTTPException(status_code=422, detail="Pacote de prioridade não está na rota.")
            for stop in stops:
                if not stop.has_coords:
                    continue
                matched = target_id in stop.delivery_ids
                stop_penalties[stop.representative_id] = 0.0 if matched else SOFT_PRIORITY_PENALTY_M
        else:
            raise HTTPException(status_code=422, detail="Prioridade inválida.")

    result = otimizar_ordem_entregas(com_coord, start=start, stop_penalties=stop_penalties)
    ordem_otimizada = list(result.get("ordem") or [])
    ordem_expandida = expand_stop_order(ordem_otimizada, stops)
    ordem_final = ordem_expandida + sem_coordenadas

    return RotasOtimizarOut(
        ordem=ordem_final,
        modo=str(result.get("modo") or "nearest_fallback"),
        sem_coordenadas=sem_coordenadas,
        distancia_total_m=result.get("distancia_total_m"),
        duracao_total_s=result.get("duracao_total_s"),
    )


# ============================================================
# POST /mobile/rotas/iniciar
# ============================================================
@router.post("/rotas/iniciar", response_model=RotasIniciarOut)
def rotas_iniciar(
    body: RotasIniciarBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Cria rota ativa com a ordem enviada. Atualiza saidas para EM_ROTA e persiste a rota."""
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    ids = body.ordem
    result = db.execute(
        select(Saida).where(
            Saida.id_saida.in_(ids),
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]),
        )
    )
    rows = result.scalars().all()
    if len(rows) != len(ids):
        raise HTTPException(
            status_code=400,
            detail="Alguma entrega não pertence ao motoboy ou não está disponível para rota.",
        )
    for s in rows:
        status_antes = s.status
        s.status = STATUS_EM_ROTA
        if status_antes == STATUS_SAIU_PARA_ENTREGA:
            db.add(
                SaidaHistorico(
                    id_saida=s.id_saida,
                    evento="em_rota",
                    status_anterior=STATUS_SAIU_PARA_ENTREGA,
                    status_novo=STATUS_EM_ROTA,
                    user_id=user.id,
                )
            )

    hoje = _hoje_operacional()
    rota = RotasMotoboy(
        motoboy_id=motoboy_id,
        data=hoje,
        status="ativa",
        ordem_json=json.dumps(ids),
        parada_atual=0,
        iniciado_em=datetime.utcnow(),
    )
    db.add(rota)
    db.commit()
    db.refresh(rota)
    return RotasIniciarOut(rota_id=str(rota.id))


# ============================================================
# GET /mobile/rotas/ativa
# ============================================================
@router.get("/rotas/ativa", response_model=Optional[RotasAtivaOut])
def rotas_ativa(
    data: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Retorna a rota ativa do motoboy (status=ativa e data=hoje). Rotas finalizadas ou de outros dias não são retornadas.
    data (opcional, YYYY-MM-DD): data local do app; quando enviado, só retorna rota desse dia."""
    motoboy_id = user.motoboy_id
    if data:
        try:
            hoje = date.fromisoformat(data.strip())
        except ValueError:
            hoje = _hoje_operacional()
    else:
        hoje = _hoje_operacional()
    # Só retorna rota realmente ativa: status=ativa, sem finalizado_em (evita dados manuais/desatualizados)
    rota = db.scalar(
        select(RotasMotoboy).where(
            RotasMotoboy.motoboy_id == motoboy_id,
            RotasMotoboy.status == "ativa",
            RotasMotoboy.data == hoje,
            RotasMotoboy.finalizado_em.is_(None),
        ).order_by(RotasMotoboy.iniciado_em.desc()).limit(1)
    )
    if not rota:
        return None
    if not refresh_active_route_if_stale(db, rota):
        return None
    ordem = json.loads(rota.ordem_json) if isinstance(rota.ordem_json, str) else rota.ordem_json
    if not isinstance(ordem, list):
        ordem = []
    if len(ordem) > 0 and rota.parada_atual >= len(ordem):
        return None
    return RotasAtivaOut(
        rota_id=str(rota.id),
        ordem=ordem,
        parada_atual=rota.parada_atual,
        data=rota.data.isoformat() if rota.data else None,
    )


# ============================================================
# POST /mobile/rotas/{id}/avancar
# ============================================================
@router.post("/rotas/{rota_id}/avancar", response_model=RotasAvancarOut)
def rotas_avancar(
    rota_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Incrementa parada_atual da rota. A rota deve pertencer ao motoboy e estar ativa."""
    motoboy_id = user.motoboy_id
    rota = db.get(RotasMotoboy, rota_id)
    if not rota or rota.motoboy_id != motoboy_id:
        raise HTTPException(status_code=404, detail="Rota não encontrada.")
    if rota.status != "ativa":
        raise HTTPException(status_code=400, detail="Rota não está ativa.")
    rota.parada_atual = rota.parada_atual + 1
    db.commit()
    db.refresh(rota)
    return RotasAvancarOut(parada_atual=rota.parada_atual)


# ============================================================
# PUT /mobile/rotas/{id}/ordem
# ============================================================
@router.put("/rotas/{rota_id}/ordem", response_model=RotasOrdemOut)
def rotas_atualizar_ordem(
    rota_id: int,
    body: RotasOrdemBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Atualiza a ordem das entregas na rota ativa. Paradas já concluídas não podem mudar."""
    motoboy_id = user.motoboy_id
    rota = db.get(RotasMotoboy, rota_id)
    if not rota or rota.motoboy_id != motoboy_id:
        raise HTTPException(status_code=404, detail="Rota não encontrada.")
    if rota.status != "ativa":
        raise HTTPException(status_code=400, detail="Rota não está ativa.")

    ordem_atual = json.loads(rota.ordem_json) if isinstance(rota.ordem_json, str) else rota.ordem_json
    if not isinstance(ordem_atual, list):
        ordem_atual = []

    nova_ordem = body.ordem
    if set(nova_ordem) != set(ordem_atual):
        raise HTTPException(
            status_code=400,
            detail="A nova ordem deve conter exatamente as mesmas entregas da rota.",
        )

    parada_atual = rota.parada_atual or 0
    if ordem_atual[:parada_atual] != nova_ordem[:parada_atual]:
        raise HTTPException(
            status_code=400,
            detail="Não é possível alterar a ordem das paradas já concluídas.",
        )

    rota.ordem_json = json.dumps(nova_ordem)
    db.commit()
    db.refresh(rota)
    return RotasOrdemOut(ordem=nova_ordem, parada_atual=parada_atual)


# ============================================================
# POST /mobile/rotas/{id}/finalizar
# ============================================================
@router.post("/rotas/{rota_id}/finalizar", status_code=204)
def rotas_finalizar(
    rota_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Marca a rota como finalizada. Após commit, GET /rotas/ativa não a retorna (filtro status=ativa)."""
    motoboy_id = user.motoboy_id
    rota = db.get(RotasMotoboy, rota_id)
    if not rota or rota.motoboy_id != motoboy_id:
        raise HTTPException(status_code=404, detail="Rota não encontrada.")
    if rota.status != "ativa":
        raise HTTPException(status_code=400, detail="Rota não está ativa.")
    rota.status = "finalizada"
    rota.finalizado_em = datetime.utcnow()
    db.commit()


# ============================================================
# GET /mobile/rotas/{id}/resumo
# ============================================================
@router.get("/rotas/{rota_id}/resumo", response_model=RotasResumoOut)
def rotas_resumo(
    rota_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Resumo operacional e financeiro de uma rota (ativa ou finalizada)."""
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")
    rota = db.get(RotasMotoboy, rota_id)
    if not rota or rota.motoboy_id != motoboy_id:
        raise HTTPException(status_code=404, detail="Rota não encontrada.")
    if rota.status not in ("ativa", "finalizada"):
        raise HTTPException(status_code=400, detail="Rota indisponível para resumo.")
    return _resumo_rota_motoboy(db, rota=rota, sub_base=sub_base, motoboy_id=motoboy_id)


# ============================================================
# GET /mobile/entrega/{id}
# ============================================================
@router.get("/entrega/{id_saida}", response_model=EntregaListItem)
def detalhe_entrega(
    id_saida: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Detalhe de uma entrega para o app."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    detail = _get_detail_for_saida(db, s.id_saida)
    ocorrencia_map = _carregar_data_hora_ocorrencia_map(db, [id_saida])
    item = _saida_to_item(s, detail, data_hora_ocorrencia=ocorrencia_map.get(id_saida))
    campos_entregue = resolve_campos_obrigatorios_ativos(
        db,
        sub_base=s.sub_base,
        servico=s.servico,
        contexto="ENTREGUE",
    )
    campos_ausente = resolve_campos_obrigatorios_ativos(
        db,
        sub_base=s.sub_base,
        servico=s.servico,
        contexto="AUSENTE",
    )
    item["campos_obrigatorios"] = sorted(set((campos_entregue or []) + (campos_ausente or [])))
    item["campos_obrigatorios_entregue"] = campos_entregue or []
    item["campos_obrigatorios_ausente"] = campos_ausente or []
    return item


# ============================================================
# POST /mobile/enderecos/sugestoes
# ============================================================
@router.post("/enderecos/sugestoes", response_model=EnderecoSugestoesResponse)
def buscar_sugestoes_endereco(
    body: EnderecoSugestoesBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    from smart_address_search import SmartAddressSearch

    hints = body.hints.model_dump() if body.hints else {}
    try:
        result = SmartAddressSearch().search(
            db=db,
            query=body.query,
            sub_base=user.sub_base or "",
            motoboy_id=user.motoboy_id,
            latitude=body.latitude,
            longitude=body.longitude,
            hints=hints,
            limit=body.limit,
            session_token=body.session_token,
        )
    except Exception:
        db_rollback_safe(db)
        logging.getLogger(__name__).exception(
            "sugestoes_endpoint_degraded sub_base=%s", user.sub_base
        )
        result = {"suggestions": [], "did_you_mean": None, "used_google": False}
    suggestions = [EnderecoSugestaoOut(**s) for s in result.get("suggestions", [])]
    return EnderecoSugestoesResponse(
        suggestions=suggestions,
        did_you_mean=result.get("did_you_mean"),
        used_google=bool(result.get("used_google")),
    )


# ============================================================
# POST /mobile/enderecos/place-details
# ============================================================
@router.post("/enderecos/place-details", response_model=PlaceDetailsResponse)
def resolver_place_details(
    body: PlaceDetailsBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    from smart_address_search import SmartAddressSearch

    hints = body.hints.model_dump() if body.hints else {}
    suggestion = SmartAddressSearch().resolve_place_details(
        db=db,
        place_id=body.place_id,
        sub_base=user.sub_base or "",
        motoboy_id=user.motoboy_id,
        query=body.query or "",
        latitude=body.latitude,
        longitude=body.longitude,
        hints=hints,
        session_token=body.session_token,
    )
    if not suggestion:
        return PlaceDetailsResponse(suggestion=None)
    return PlaceDetailsResponse(suggestion=EnderecoSugestaoOut(**suggestion))


# ============================================================
# PUT /mobile/entrega/{id_saida}/endereco
# ============================================================
SUGGESTION_ORIGINS = frozenset({"suggestion", "autocomplete", "google_places"})
REVALIDATE_COORDS_DISTANCE_M = 100.0

PRECISION_RANK = {"approx": 0, "street": 1, "rooftop": 2}


def _precision_rank(precision: Optional[str]) -> int:
    return PRECISION_RANK.get((precision or "").strip().lower(), -1)


def _should_replace_client_coords(
    *,
    origem: str,
    client_precision: Optional[str],
    dist_m: float,
    server_precision: str,
) -> bool:
    """Só substitui coords do cliente por resultado strict validado (cidade/estado/CEP conferidos)."""
    if origem == "google_places" or client_precision == "rooftop":
        return False
    if dist_m <= REVALIDATE_COORDS_DISTANCE_M:
        return False
    return _precision_rank(server_precision) >= _precision_rank(client_precision)


@router.put("/entrega/{id_saida}/endereco", response_model=EntregaListItem)
def atualizar_endereco(
    id_saida: int,
    body: EnderecoBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Atualiza endereço da entrega (SaidaDetail). Cria detail se não existir."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    detail = _get_detail_for_saida(db, id_saida)
    origem = (body.origem or "manual").strip().lower()
    if origem not in ("manual", "ocr", "voz", "suggestion", "autocomplete", "mapa", "google_places"):
        origem = "manual"
    parts = [body.rua, body.numero, body.complemento, body.bairro, body.cidade, body.estado, body.cep]
    endereco_formatado = ", ".join(p for p in parts if p)

    log = logging.getLogger(__name__)

    def _valid_client_coords(lat_v: Optional[float], lon_v: Optional[float]) -> bool:
        if lat_v is None or lon_v is None:
            return False
        try:
            lat_f = float(lat_v)
            lon_f = float(lon_v)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(lat_f) or not math.isfinite(lon_f):
            return False
        if lat_f < -90 or lat_f > 90 or lon_f < -180 or lon_f > 180:
            return False
        if lat_f == 0 and lon_f == 0:
            return False
        return True

    lat = body.latitude
    lon = body.longitude
    latlon_from_client = _valid_client_coords(lat, lon)
    geocode_called = False
    coord_precision = (body.coord_precision or "").strip().lower() or None
    if coord_precision not in ("rooftop", "street", "approx"):
        coord_precision = None
    geocode_source = (body.geocode_source or "").strip() or None
    geocode_score = float(body.geocode_score) if body.geocode_score is not None else None
    geocoded_at = None

    if latlon_from_client:
        lat = float(lat)
        lon = float(lon)
        if origem in SUGGESTION_ORIGINS:
            strict = geocode_address_strict(
                rua=body.rua,
                numero=body.numero,
                bairro=body.bairro,
                cidade=body.cidade,
                estado=body.estado,
                cep=body.cep,
                db=db,
            )
            if strict:
                s_lat, s_lon, s_prec, s_source, s_score = strict
                dist_m = haversine_m(lat, lon, s_lat, s_lon)
                if _should_replace_client_coords(
                    origem=origem,
                    client_precision=coord_precision,
                    dist_m=dist_m,
                    server_precision=s_prec,
                ):
                    lat, lon = s_lat, s_lon
                    coord_precision = s_prec
                    geocode_source = s_source
                    geocode_score = s_score
                    geocode_called = True
                    log.info(
                        "endereco_save replaced suggestion coords with strict dist_m=%.0f id_saida=%s",
                        dist_m,
                        id_saida,
                    )
                elif dist_m > REVALIDATE_COORDS_DISTANCE_M:
                    log.warning(
                        "endereco_save kept client coords dist_m=%.0f id_saida=%s origem=%s",
                        dist_m,
                        id_saida,
                        origem,
                    )
                elif s_prec == "rooftop" and _precision_rank(coord_precision) < _precision_rank("rooftop"):
                    # Strict confirmou o número da casa perto das coords do cliente:
                    # mantém coords do cliente, promove precisão.
                    coord_precision = "rooftop"
                    geocode_source = s_source
                    geocode_score = s_score
                    log.info(
                        "endereco_save upgraded precision to rooftop dist_m=%.0f id_saida=%s",
                        dist_m,
                        id_saida,
                    )
                elif coord_precision is None:
                    coord_precision = s_prec if s_prec != "approx" else "street"
            # strict None → manter coords do cliente (nunca degradar por geocode fraco)
        if coord_precision is None:
            if origem in ("mapa", "google_places"):
                coord_precision = "rooftop"
            elif origem in ("suggestion", "autocomplete"):
                coord_precision = "street"
            else:
                coord_precision = "approx"
        if not geocode_source:
            geocode_source = origem if origem in ("google_places", "mapa") else (body.geocode_source or origem)
        if geocode_score is None and coord_precision == "rooftop":
            geocode_score = 95.0
        elif geocode_score is None and coord_precision == "street":
            geocode_score = 75.0
        geocoded_at = datetime.now()
        log.info(
            "endereco_save latlon_from_client=true origem=%s id_saida=%s geocode_called=%s precision=%s",
            origem,
            id_saida,
            geocode_called,
            coord_precision,
        )
    elif endereco_formatado.strip():
        geocode_called = True
        strict = geocode_address_strict(
            rua=body.rua,
            numero=body.numero,
            bairro=body.bairro,
            cidade=body.cidade,
            estado=body.estado,
            cep=body.cep,
            db=db,
        )
        if strict:
            lat, lon, coord_precision, geocode_source, geocode_score = strict
            geocoded_at = datetime.now()
            log.info(
                "endereco_save strict ok id_saida=%s lat=%s lon=%s precision=%s",
                id_saida,
                lat,
                lon,
                coord_precision,
            )
        else:
            lat, lon = None, None
            coord_precision = None
            geocode_source = None
            geocode_score = None
            log.warning(
                "endereco_save strict rejected coords id_saida=%s endereco=%s",
                id_saida,
                endereco_formatado[:80],
            )
    else:
        log.info(
            "endereco_save latlon_from_client=false origem=%s id_saida=%s geocode_called=false",
            origem,
            id_saida,
        )

    if detail:
        detail.dest_nome = body.destinatario.strip()
        detail.dest_rua = body.rua.strip()
        detail.dest_numero = str(body.numero).strip()
        detail.dest_complemento = (body.complemento or "").strip() or None
        detail.dest_bairro = body.bairro.strip()
        detail.dest_cidade = body.cidade.strip()
        detail.dest_estado = body.estado.strip()
        detail.dest_cep = body.cep.strip()
        detail.endereco_formatado = endereco_formatado
        detail.endereco_origem = origem
        detail.coord_precision = coord_precision
        detail.geocode_source = geocode_source
        detail.geocode_score = geocode_score
        detail.geocoded_at = geocoded_at
        if lat is not None:
            detail.latitude = lat
        else:
            detail.latitude = None
        if lon is not None:
            detail.longitude = lon
        else:
            detail.longitude = None
    else:
        detail = SaidaDetail(
            id_saida=id_saida,
            id_entregador=user.motoboy_id,
            status=s.status or STATUS_EM_ROTA,
            tentativa=1,
            dest_nome=body.destinatario.strip(),
            dest_rua=body.rua.strip(),
            dest_numero=str(body.numero).strip(),
            dest_complemento=(body.complemento or "").strip() or None,
            dest_bairro=body.bairro.strip(),
            dest_cidade=body.cidade.strip(),
            dest_estado=body.estado.strip(),
            dest_cep=body.cep.strip(),
            endereco_formatado=endereco_formatado,
            endereco_origem=origem,
            coord_precision=coord_precision,
            geocode_source=geocode_source,
            geocode_score=geocode_score,
            geocoded_at=geocoded_at,
            latitude=lat,
            longitude=lon,
        )
        db.add(detail)
    db.commit()
    db.refresh(detail)

    if lat is not None and lon is not None:
        try:
            from known_addresses_service import upsert_from_save
            from address_telemetry import log_address_event

            upsert_from_save(
                db,
                sub_base=user.sub_base or "",
                motoboy_id=user.motoboy_id,
                rua=body.rua.strip(),
                numero=str(body.numero).strip(),
                bairro=body.bairro.strip(),
                cidade=body.cidade.strip(),
                estado=body.estado.strip(),
                cep=body.cep.strip(),
                latitude=float(lat),
                longitude=float(lon),
            )
            log_address_event(
                db,
                "address_saved_success",
                user.sub_base,
                user.motoboy_id,
                endereco_formatado,
                {"id_saida": id_saida, "origem": origem},
            )
        except Exception as e:
            log.warning("known_address upsert failed: %s", e)
    else:
        try:
            from address_telemetry import log_address_event

            log_address_event(
                db,
                "address_saved_failure",
                user.sub_base,
                user.motoboy_id,
                endereco_formatado,
                {"id_saida": id_saida, "reason": "no_coords"},
            )
        except Exception:
            pass

    return _saida_to_item(s, detail)


def _validar_saida_para_finalizacao_lote(
    s: Optional[Saida],
    acao: str,
    motoboy_id: int,
    sub_base: str,
) -> Optional[str]:
    if not s or (s.sub_base or "").strip() != (sub_base or "").strip() or s.motoboy_id != motoboy_id:
        return "Pedido não encontrado ou não pertence a você"
    status_norm = normalizar_status_saida(s.status)
    if _status_esta_finalizado(status_norm):
        return "Pedido já finalizado"
    if status_norm == STATUS_AUSENTE and acao == "entregue":
        return "Pedido já marcado como ausente"
    if status_norm not in (STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA):
        return "Status não permite finalização"
    return None


def _aplicar_entregue_lote(
    db: Session,
    s: Saida,
    user: User,
    status_anterior: str,
) -> None:
    s.status = STATUS_ENTREGUE
    s.data_hora_entrega = datetime.utcnow()
    db.add(
        SaidaHistorico(
            id_saida=int(s.id_saida),
            evento="entregue_lote",
            status_anterior=status_anterior,
            status_novo=STATUS_ENTREGUE,
            user_id=user.id,
        )
    )


def _aplicar_ausente_lote(
    db: Session,
    s: Saida,
    user: User,
    motivo: MotivoAusencia,
    observacao: Optional[str],
    detail: Optional[SaidaDetail],
    status_anterior: str,
) -> None:
    s.status = STATUS_AUSENTE
    obs = (observacao or "").strip() or None
    if detail:
        detail.motivo_ocorrencia = motivo.descricao
        detail.observacao_ocorrencia = obs
    else:
        db.add(
            SaidaDetail(
                id_saida=int(s.id_saida),
                id_entregador=int(user.motoboy_id or 0),
                status=STATUS_AUSENTE,
                motivo_ocorrencia=motivo.descricao,
                observacao_ocorrencia=obs,
            )
        )
    db.add(
        SaidaHistorico(
            id_saida=int(s.id_saida),
            evento="ausente_lote",
            status_anterior=status_anterior,
            status_novo=STATUS_AUSENTE,
            user_id=user.id,
        )
    )


# ============================================================
# POST /mobile/entregas/finalizar-lote
# ============================================================
@router.post("/entregas/finalizar-lote", response_model=FinalizarLoteResponse)
def finalizar_lote(
    body: FinalizarLoteBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Finaliza pedidos em lote (parcial). Backend valida campos obrigatórios por pedido."""
    motivo: Optional[MotivoAusencia] = None
    if body.acao == "ausente":
        if body.motivo_id is None:
            raise HTTPException(status_code=422, detail="motivo_id é obrigatório para ausência em lote.")
        motivo = db.get(MotivoAusencia, body.motivo_id)
        if not motivo or not motivo.ativo:
            raise HTTPException(status_code=422, detail="Motivo de ausência inválido.")
        if motivo.descricao.strip().lower() == "outro" and not (body.observacao or "").strip():
            raise HTTPException(status_code=422, detail="Observação obrigatória quando motivo é 'Outro'.")

    ids_unicos = list(dict.fromkeys(int(i) for i in body.ids))
    details_map = _carregar_details_por_saida_ids(db, ids_unicos)

    active_route_ids = get_active_route_delivery_ids(db, user.motoboy_id)
    active_route_set = set(active_route_ids or [])

    finalizados: List[FinalizarLoteItemOut] = []
    bloqueados: List[FinalizarLoteBloqueadoOut] = []
    erros: List[FinalizarLoteErroOut] = []

    for id_saida in ids_unicos:
        try:
            if id_saida in active_route_set:
                s_preview = db.get(Saida, id_saida)
                bloqueados.append(
                    FinalizarLoteBloqueadoOut(
                        id_saida=id_saida,
                        codigo=getattr(s_preview, "codigo", None) if s_preview else None,
                        motivo="Pedido faz parte da rota ativa. Finalize pela rota ou individualmente.",
                    )
                )
                continue

            s = db.get(Saida, id_saida)
            bloqueio = _validar_saida_para_finalizacao_lote(
                s, body.acao, user.motoboy_id, user.sub_base
            )
            if bloqueio:
                bloqueados.append(
                    FinalizarLoteBloqueadoOut(
                        id_saida=id_saida,
                        codigo=getattr(s, "codigo", None) if s else None,
                        motivo=bloqueio,
                    )
                )
                continue

            assert s is not None
            detail = details_map.get(id_saida) or _get_detail_for_saida(db, id_saida)
            status_anterior = normalizar_status_saida(s.status) or (s.status or STATUS_EM_ROTA)

            if body.acao == "entregue":
                faltantes = validate_campos_obrigatorios_conclusao(
                    db,
                    saida=s,
                    contexto="ENTREGUE",
                    detail=detail,
                    overrides=None,
                )
                if faltantes:
                    bloqueados.append(
                        FinalizarLoteBloqueadoOut(
                            id_saida=id_saida,
                            codigo=s.codigo,
                            motivo=format_bloqueio_motivo(faltantes),
                        )
                    )
                    continue
                _aplicar_entregue_lote(db, s, user, status_anterior)
                status_novo = STATUS_ENTREGUE
            else:
                assert motivo is not None
                overrides = (
                    {"observacao_ocorrencia": body.observacao}
                    if body.observacao is not None
                    else None
                )
                faltantes = validate_campos_obrigatorios_conclusao(
                    db,
                    saida=s,
                    contexto="AUSENTE",
                    detail=detail,
                    overrides=overrides,
                )
                if faltantes:
                    bloqueados.append(
                        FinalizarLoteBloqueadoOut(
                            id_saida=id_saida,
                            codigo=s.codigo,
                            motivo=format_bloqueio_motivo(faltantes),
                        )
                    )
                    continue
                _aplicar_ausente_lote(db, s, user, motivo, body.observacao, detail, status_anterior)
                status_novo = STATUS_AUSENTE

            db.commit()
            db.refresh(s)
            finalizados.append(FinalizarLoteItemOut(id_saida=id_saida, status=status_novo))
        except Exception as exc:
            db.rollback()
            logging.exception("finalizar_lote id_saida=%s", id_saida)
            erros.append(FinalizarLoteErroOut(id_saida=id_saida, mensagem=str(exc)))

    rota_sync: Optional[RotaSyncOut] = None
    if finalizados:
        sync_raw = sync_active_route_after_delivery_update(
            db, user.motoboy_id, finalizados[-1].id_saida
        )
        if sync_raw.get("in_active_route") or sync_raw.get("rota_finalizada"):
            rota_sync = RotaSyncOut(**sync_raw)

    return FinalizarLoteResponse(
        finalizados=finalizados,
        bloqueados=bloqueados,
        erros=erros,
        rota_sync=rota_sync,
    )


# ============================================================
# POST /mobile/entrega/{id}/entregue
# ============================================================
@router.post("/entrega/{id_saida}/entregue")
def marcar_entregue(
    id_saida: int,
    body: Optional[EntregueBody] = Body(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Marca entrega como ENTREGUE e registra data_hora_entrega. Só permite se status for EM_ROTA.
    Se body for enviado, preenche tipo_recebedor, nome_recebedor, tipo_documento, numero_documento, observacao_entrega em saidas_detail."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    status_norm = normalizar_status_saida(s.status)

    def _set_if_present(detail: SaidaDetail) -> None:
        if not body:
            return
        if body.tipo_recebedor is not None:
            detail.tipo_recebedor = (body.tipo_recebedor or "").strip() or None
        if body.nome_recebedor is not None:
            detail.nome_recebedor = (body.nome_recebedor or "").strip() or None
        if body.tipo_documento is not None:
            detail.tipo_documento = (body.tipo_documento or "").strip() or None
        if body.numero_documento is not None:
            detail.numero_documento = (body.numero_documento or "").strip() or None
        if body.observacao_entrega is not None:
            detail.observacao_entrega = (body.observacao_entrega or "").strip() or None

    def _upsert_detail_com_body() -> None:
        if not body:
            return
        detail = _get_detail_for_saida(db, id_saida)
        if detail:
            _set_if_present(detail)
        else:
            detail = SaidaDetail(
                id_saida=id_saida,
                id_entregador=user.motoboy_id,
                status=s.status or STATUS_EM_ROTA,
                tentativa=1,
            )
            _set_if_present(detail)
            db.add(detail)

    if _status_esta_finalizado(status_norm):
        if status_norm == STATUS_ENTREGUE and body:
            _upsert_detail_com_body()
            db.commit()
            db.refresh(s)
            return _marcacao_response(db, s, complemento=True)
        raise HTTPException(status_code=422, detail=_status_finalizado_detail(s, status_norm))
    if status_norm == STATUS_SAIU_PARA_ENTREGA:
        raise HTTPException(
            status_code=422,
            detail="Inicie a rota antes de finalizar entregas.",
        )

    _upsert_detail_com_body()

    detail_atual = _get_detail_for_saida(db, id_saida)
    overrides = {}
    if body:
        if body.tipo_recebedor is not None:
            overrides["tipo_recebedor"] = body.tipo_recebedor
        if body.nome_recebedor is not None:
            overrides["nome_recebedor"] = body.nome_recebedor
        if body.numero_documento is not None:
            overrides["numero_documento"] = body.numero_documento
        if body.observacao_entrega is not None:
            overrides["observacao_entrega"] = body.observacao_entrega
    faltantes = validate_campos_obrigatorios_conclusao(
        db,
        saida=s,
        contexto="ENTREGUE",
        detail=detail_atual,
        overrides=overrides,
    )
    raise_if_campos_obrigatorios_faltando(faltantes)

    s.status = STATUS_ENTREGUE
    s.data_hora_entrega = datetime.utcnow()  # deprecated: mantido em transição; ver saida_historico evento "entregue"
    db.add(
        SaidaHistorico(
            id_saida=id_saida,
            evento="entregue",
            status_anterior=STATUS_EM_ROTA,
            status_novo=STATUS_ENTREGUE,
            user_id=user.id,
        )
    )
    db.commit()
    db.refresh(s)
    return _marcacao_response(db, s, motoboy_id=user.motoboy_id)


# ============================================================
# POST /mobile/entrega/{id}/ausente
# ============================================================
@router.post("/entrega/{id_saida}/ausente")
def marcar_ausente(
    id_saida: int,
    body: AusenteBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Marca entrega como AUSENTE com motivo. Só permite se status for EM_ROTA."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    status_norm = normalizar_status_saida(s.status)
    if _status_esta_finalizado(status_norm):
        raise HTTPException(status_code=422, detail=_status_finalizado_detail(s, status_norm))
    if status_norm == STATUS_SAIU_PARA_ENTREGA:
        raise HTTPException(
            status_code=422,
            detail="Inicie a rota antes de finalizar entregas.",
        )
    motivo = db.get(MotivoAusencia, body.motivo_id)
    if not motivo or not motivo.ativo:
        raise HTTPException(status_code=422, detail="Motivo de ausência inválido.")
    if motivo.descricao.strip().lower() == "outro" and not (body.observacao or "").strip():
        raise HTTPException(status_code=422, detail="Observação obrigatória quando motivo é 'Outro'.")

    detail_atual = _get_detail_for_saida(db, id_saida)
    faltantes = validate_campos_obrigatorios_conclusao(
        db,
        saida=s,
        contexto="AUSENTE",
        detail=detail_atual,
        overrides={"observacao_ocorrencia": body.observacao} if body.observacao is not None else None,
    )
    raise_if_campos_obrigatorios_faltando(faltantes)

    s.status = STATUS_AUSENTE
    detail = _get_detail_for_saida(db, id_saida)
    if detail:
        detail.motivo_ocorrencia = motivo.descricao
        detail.observacao_ocorrencia = (body.observacao or "").strip() or None
    else:
        detail = SaidaDetail(
            id_saida=id_saida,
            id_entregador=0,
            status=STATUS_AUSENTE,
            motivo_ocorrencia=motivo.descricao,
            observacao_ocorrencia=(body.observacao or "").strip() or None,
        )
        db.add(detail)
    db.add(
        SaidaHistorico(
            id_saida=id_saida,
            evento="ausente",
            status_anterior=STATUS_EM_ROTA,
            status_novo=STATUS_AUSENTE,
            user_id=user.id,
        )
    )
    db.commit()
    db.refresh(s)
    return _marcacao_response(db, s, motoboy_id=user.motoboy_id)


# ============================================================
# POST /mobile/entrega/{id}/nova-tentativa
# ============================================================
@router.post("/entrega/{id_saida}/nova-tentativa")
def nova_tentativa(
    id_saida: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Coloca pedido AUSENTE de volta em SAIU_PARA_ENTREGA e incrementa tentativa."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    status_norm = normalizar_status_saida(s.status)
    if status_norm != STATUS_AUSENTE:
        raise HTTPException(status_code=422, detail="Só é possível nova tentativa para entregas ausentes.")
    s.status = STATUS_SAIU_PARA_ENTREGA
    detail = _get_detail_for_saida(db, id_saida)
    if detail:
        detail.tentativa = (detail.tentativa or 1) + 1
    else:
        detail = SaidaDetail(
            id_saida=id_saida,
            id_entregador=user.motoboy_id,
            status=STATUS_SAIU_PARA_ENTREGA,
            tentativa=2,
        )
        db.add(detail)
    db.add(
        SaidaHistorico(
            id_saida=id_saida,
            evento="nova_tentativa",
            status_anterior=STATUS_AUSENTE,
            status_novo=STATUS_SAIU_PARA_ENTREGA,
            user_id=user.id,
        )
    )
    db.commit()
    return {"ok": True, "id_saida": id_saida, "tentativa": detail.tentativa}


# ============================================================
# GET /mobile/motivos-ausencia
# ============================================================
@router.get("/motivos-ausencia", response_model=List[MotivoAusenciaOut])
def listar_motivos_ausencia(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Lista motivos de ausência ativos para o combo."""
    rows = db.scalars(
        select(MotivoAusencia).where(MotivoAusencia.ativo.is_(True)).order_by(MotivoAusencia.id)
    ).all()
    return [MotivoAusenciaOut(id=r.id, descricao=r.descricao) for r in rows]


# ============================================================
# POST /mobile/scan — leituras sequenciais (igual web): INSERT novo ou atribui existente
# ============================================================
def _nome_motoboy_atual(db: Session, saida: Saida) -> str:
    if not saida or not saida.motoboy_id:
        return ""
    motoboy = db.get(Motoboy, saida.motoboy_id)
    if not motoboy:
        return ""
    nome = (_get_motoboy_nome(db, motoboy) or "").strip()
    return nome


def _owner_valor_por_sub_base(db: Session, user: User, sub_base: str) -> Decimal:
    raw = getattr(user, "owner_valor", Decimal("0")) or Decimal("0")
    try:
        valor = Decimal(str(raw))
    except Exception:
        valor = Decimal("0")
    if valor > 0:
        return valor
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if not owner:
        return Decimal("0")
    return Decimal(getattr(owner, "valor", 0) or 0)


def _garantir_cobranca_owner_saida(db: Session, saida: Saida, owner_valor: Decimal) -> None:
    ja_cobrado = db.scalar(
        select(exists().where(
            OwnerCobrancaItem.id_saida == saida.id_saida,
            OwnerCobrancaItem.cancelado.is_(False),
        ))
    )
    if ja_cobrado:
        return
    db.add(
        OwnerCobrancaItem(
            sub_base=saida.sub_base or "",
            id_coleta=None,
            id_saida=saida.id_saida,
            valor=owner_valor,
        )
    )


def _scan_origem(raw: Optional[str]) -> str:
    origem = (raw or "camera").strip().lower()
    if origem not in ("camera", "manual"):
        return "camera"
    return origem


@router.post("/scan")
def scan_codigo(
    body: ScanBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_mobile_scan_user),
):
    """
    Leituras sequenciais (igual web): se código não existe -> INSERT novo e atribui ao motoboy.
    Se existe: valida status (não permite cancelado, entregue, em_rota de outro).
    Retorna status na resposta de erro quando bloqueia por status.
    """
    raw = body.codigo.strip()
    sub_base = user.sub_base
    role = int(getattr(user, "role", 0) or 0)
    motoboy_id = getattr(user, "motoboy_id", None) if role == 4 else None
    status_scan = STATUS_SAIU_PARA_ENTREGA if motoboy_id else "saiu"
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")
    owner_valor = _owner_valor_por_sub_base(db, user, sub_base)

    origem = _scan_origem(getattr(body, "origem", None))
    strict_qr = origem == "camera"
    if strict_qr and not is_qr_like_scan_payload(raw):
        raise HTTPException(
            status_code=422,
            detail="Leitura inválida pela câmera. Use apenas QRCode da etiqueta.",
        )

    codigo, servico, qr_payload_raw = normalize_codigo(raw, strict_qr=strict_qr)
    if codigo is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "QRCode inválido. Leia novamente o QR da etiqueta."
                if strict_qr
                else "Código inválido. Verifique o formato do QR/código de barras."
            ),
        )

    saida = db.scalar(
        select(Saida).where(
            Saida.codigo == codigo,
            Saida.sub_base == sub_base,
        ).with_for_update()
    )

    # ——— Código não existe: registrar como novo (leitura sequencial, igual web) ———
    if not saida:
        motoboy = db.get(Motoboy, motoboy_id) if motoboy_id else None
        entregador_nome = _get_motoboy_nome(db, motoboy) if motoboy else (user.username or "Operacao Mobile")
        servico_val = canonicalize_servico(servico)
        qr_raw = qr_payload_raw.strip() if (qr_payload_raw and _should_store_qr_payload_raw(servico_val, qr_payload_raw)) else None
        try:
            nova = Saida(
                sub_base=sub_base,
                username=user.username,
                entregador=entregador_nome,
                entregador_id=None,
                motoboy_id=motoboy_id,
                codigo=codigo,
                servico=servico_val,
                status=status_scan,
                qr_payload_raw=qr_raw or None,
            )
            db.add(nova)
            db.flush()
            _garantir_cobranca_owner_saida(db, nova, owner_valor)
            db.add(
                SaidaHistorico(
                    id_saida=nova.id_saida,
                    evento="scan",
                    status_novo=status_scan,
                    user_id=user.id,
                )
            )
            db.commit()
            db.refresh(nova)
            detail = _get_detail_for_saida(db, nova.id_saida)
            return {"ok": True, "conflito": False, "ja_existia": False, "entrega": _saida_to_item(nova, detail)}
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Erro ao registrar leitura: {e}")

    # ——— Existe: validar status (não permitir cancelado, entregue, em_rota de outro) ———
    status_norm = normalizar_status_saida(saida.status)

    if _status_esta_finalizado(status_norm):
        registrar_log_leitura_critico(
            sub_base=sub_base,
            username=getattr(user, "username", None),
            origem=origem,
            tipo="saida",
            codigo=saida.codigo,
            resultado="bloqueio_status_finalizado",
            role=role,
            motoboy_id=motoboy_id,
            id_saida=saida.id_saida,
            origem_app="mobile",
            endpoint="/mobile/scan",
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": "STATUS_FINALIZADO",
                "id_saida": saida.id_saida,
                "status_atual": saida.status,
                "message": f"Pedido com status finalizado: {saida.status}.",
            },
        )

    # Em rota / saiu:
    # - staff segue sem conflito
    # - mesmo motoboy segue sem conflito
    # - sem titular (motoboy_id nulo) segue leitura normal (reatribui sem conflito)
    # - outro motoboy titular: conflito 409 para confirmar assumir
    if status_norm in (STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA, "saiu"):
        if motoboy_id is None:
            if qr_payload_raw and _should_store_qr_payload_raw(servico or "", qr_payload_raw):
                if not saida.qr_payload_raw or not saida.qr_payload_raw.strip():
                    saida.qr_payload_raw = qr_payload_raw.strip()
            _garantir_cobranca_owner_saida(db, saida, owner_valor)
            db.commit()
            db.refresh(saida)
            detail = _get_detail_for_saida(db, saida.id_saida)
            registrar_log_leitura_critico(
                sub_base=sub_base,
                username=getattr(user, "username", None),
                origem=origem,
                tipo="saida",
                codigo=saida.codigo,
                resultado="duplicado",
                role=role,
                motoboy_id=None,
                id_saida=saida.id_saida,
                origem_app="mobile",
                endpoint="/mobile/scan",
            )
            return {"ok": True, "conflito": False, "ja_existia": True, "entrega": _saida_to_item(saida, detail)}
        if saida.motoboy_id == motoboy_id:
            data_operacional = _ctx_data_operacional_saida(db, saida)
            hoje = _hoje_operacional()
            if data_operacional < hoje:
                registrar_log_leitura_critico(
                    sub_base=sub_base,
                    username=getattr(user, "username", None),
                    origem=origem,
                    tipo="saida",
                    codigo=saida.codigo,
                    resultado="leitura_dia_anterior_aguardando_confirmacao",
                    role=role,
                    motoboy_id=motoboy_id,
                    id_saida=saida.id_saida,
                    origem_app="mobile",
                    endpoint="/mobile/scan",
                )
                return JSONResponse(
                    status_code=409,
                    content={
                        "code": "LEITURA_DIA_ANTERIOR",
                        "id_saida": saida.id_saida,
                        "data_operacional_anterior": data_operacional.isoformat(),
                        "status_atual": saida.status,
                        "motoboy_id": saida.motoboy_id,
                        "motoboy_nome": _nome_motoboy_atual(db, saida) or saida.entregador or "",
                        "conflito": False,
                    },
                )
            if qr_payload_raw and _should_store_qr_payload_raw(servico or "", qr_payload_raw):
                if not saida.qr_payload_raw or not saida.qr_payload_raw.strip():
                    saida.qr_payload_raw = qr_payload_raw.strip()
            _garantir_cobranca_owner_saida(db, saida, owner_valor)
            db.commit()
            detail = _get_detail_for_saida(db, saida.id_saida)
            registrar_log_leitura_critico(
                sub_base=sub_base,
                username=getattr(user, "username", None),
                origem=origem,
                tipo="saida",
                codigo=saida.codigo,
                resultado="duplicado",
                role=role,
                motoboy_id=motoboy_id,
                id_saida=saida.id_saida,
                origem_app="mobile",
                endpoint="/mobile/scan",
            )
            return {"ok": True, "conflito": False, "ja_existia": True, "entrega": _saida_to_item(saida, detail)}
        if saida.motoboy_id is not None:
            _garantir_cobranca_owner_saida(db, saida, owner_valor)
            db.commit()
            nome_atual = _nome_motoboy_atual(db, saida) or "outro motoboy"
            registrar_log_leitura_critico(
                sub_base=sub_base,
                username=getattr(user, "username", None),
                origem=origem,
                tipo="saida",
                codigo=saida.codigo,
                resultado="atribuido_a_outro",
                role=role,
                motoboy_id=motoboy_id,
                id_saida=saida.id_saida,
                origem_app="mobile",
                endpoint="/mobile/scan",
            )
            return JSONResponse(
                status_code=409,
                content={
                    "conflito": True,
                    "motoboy_atual": nome_atual,
                    "id_saida": saida.id_saida,
                },
            )
        # sem titular (motoboy_id nulo): segue para reatribuição sem conflito

    # Coletado ou AUSENTE ou outro: atribuir ao motoboy logado
    if qr_payload_raw and _should_store_qr_payload_raw(servico or "", qr_payload_raw):
        if not saida.qr_payload_raw or not saida.qr_payload_raw.strip():
            saida.qr_payload_raw = qr_payload_raw.strip()
    motoboy_id_anterior = saida.motoboy_id
    status_anterior = status_norm
    if motoboy_id is not None:
        motoboy = db.get(Motoboy, motoboy_id)
        if motoboy:
            saida.entregador = _get_motoboy_nome(db, motoboy)
            saida.entregador_id = None
    saida.motoboy_id = motoboy_id
    saida.status = status_scan
    if status_norm == STATUS_AUSENTE:
        detail = _get_detail_for_saida(db, saida.id_saida)
        if detail:
            detail.tentativa = (detail.tentativa or 1) + 1
        else:
            db.add(
                SaidaDetail(
                    id_saida=saida.id_saida,
                    id_entregador=motoboy_id,
                    status=status_scan,
                    tentativa=2,
                )
            )
    _garantir_cobranca_owner_saida(db, saida, owner_valor)
    db.add(
        SaidaHistorico(
            id_saida=saida.id_saida,
            evento="assumir",
            status_anterior=status_norm,
            status_novo=status_scan,
            motoboy_id_anterior=motoboy_id_anterior,
            motoboy_id_novo=motoboy_id,
            user_id=user.id,
        )
    )
    db.commit()
    db.refresh(saida)
    detail = _get_detail_for_saida(db, saida.id_saida)
    houve_atribuicao_ou_progresso = bool(
        motoboy_id is not None
        and (
            motoboy_id_anterior != motoboy_id
            or status_anterior != status_scan
        )
    )
    return {
        "ok": True,
        "conflito": False,
        "ja_existia": not houve_atribuicao_ou_progresso,
        "entrega": _saida_to_item(saida, detail),
    }


@router.post("/entrega/{id_saida}/confirmar-nova-saida-mesmo-entregador")
def confirmar_nova_saida_mesmo_entregador_mobile(
    id_saida: int,
    body: ConfirmarNovaSaidaMesmoEntregadorBody = Body(default=ConfirmarNovaSaidaMesmoEntregadorBody()),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_mobile_scan_user),
):
    sub_base = user.sub_base
    role = int(getattr(user, "role", 0) or 0)
    motoboy_id = getattr(user, "motoboy_id", None) if role == 4 else None
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    saida = db.scalar(
        select(Saida).where(
            Saida.id_saida == id_saida,
            Saida.sub_base == sub_base,
        )
    )
    if not saida:
        raise HTTPException(status_code=404, detail="Saída não encontrada.")

    status_norm = normalizar_status_saida(saida.status)
    if _status_esta_finalizado(status_norm):
        registrar_log_leitura_critico(
            sub_base=sub_base,
            username=getattr(user, "username", None),
            origem="desconhecida",
            tipo="saida",
            codigo=saida.codigo,
            resultado="bloqueio_status_finalizado",
            role=role,
            motoboy_id=motoboy_id,
            id_saida=saida.id_saida,
            origem_app="mobile",
            endpoint="/mobile/entrega/{id_saida}/confirmar-nova-saida-mesmo-entregador",
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": "STATUS_FINALIZADO",
                "id_saida": saida.id_saida,
                "status_atual": saida.status,
                "message": f"Pedido com status finalizado: {saida.status}.",
            },
        )

    if motoboy_id is not None and saida.motoboy_id is not None and saida.motoboy_id != motoboy_id:
        nome_atual = _nome_motoboy_atual(db, saida) or "outro motoboy"
        return JSONResponse(
            status_code=409,
            content={
                "code": "TROCA_ENTREGADOR",
                "conflito": True,
                "motoboy_atual": nome_atual,
                "id_saida": saida.id_saida,
            },
        )

    data_operacional_anterior = _ctx_data_operacional_saida(db, saida)
    hoje = _hoje_operacional()
    if data_operacional_anterior >= hoje:
        registrar_log_leitura_critico(
            sub_base=sub_base,
            username=getattr(user, "username", None),
            origem="desconhecida",
            tipo="saida",
            codigo=saida.codigo,
            resultado="duplicado",
            role=role,
            motoboy_id=motoboy_id,
            id_saida=saida.id_saida,
            origem_app="mobile",
            endpoint="/mobile/entrega/{id_saida}/confirmar-nova-saida-mesmo-entregador",
        )
        detail = _get_detail_for_saida(db, saida.id_saida)
        return {"ok": True, "conflito": False, "ja_existia": True, "entrega": _saida_to_item(saida, detail)}

    payload_hist = _payload_nova_saida_mesmo_entregador(
        data_operacional_anterior=data_operacional_anterior,
        data_operacional_nova=hoje,
        id_motoboy=saida.motoboy_id,
        confirmado_por=getattr(user, "username", None),
        origem=(body.origem or "mobile"),
    )
    db.add(
        SaidaHistorico(
            id_saida=saida.id_saida,
            evento="nova_saida_mesmo_entregador",
            status_anterior=saida.status,
            status_novo=saida.status,
            motoboy_id_anterior=saida.motoboy_id,
            motoboy_id_novo=saida.motoboy_id,
            user_id=getattr(user, "id", None),
            payload=payload_hist,
        )
    )
    try:
        db.commit()
        db.refresh(saida)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Erro ao confirmar nova saída.")

    registrar_log_leitura_critico(
        sub_base=sub_base,
        username=getattr(user, "username", None),
        origem="desconhecida",
        tipo="saida",
        codigo=saida.codigo,
        resultado="nova_saida_mesmo_entregador_confirmada",
        role=role,
        motoboy_id=motoboy_id,
        id_saida=saida.id_saida,
        origem_app="mobile",
        endpoint="/mobile/entrega/{id_saida}/confirmar-nova-saida-mesmo-entregador",
    )
    detail = _get_detail_for_saida(db, saida.id_saida)
    return {"ok": True, "conflito": False, "ja_existia": False, "entrega": _saida_to_item(saida, detail)}


# ============================================================
# DELETE /mobile/entrega/{id}
# ============================================================
@router.delete("/entrega/{id_saida}", status_code=204)
def deletar_entrega_mobile(
    id_saida: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """
    Remove definitivamente a saída do próprio motoboy.
    Reaproveita a mesma regra de janela de exclusão (24h) da web.
    """
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    status_norm = normalizar_status_saida(s.status)
    if _status_esta_finalizado(status_norm):
        raise HTTPException(status_code=422, detail=_status_finalizado_detail(s, status_norm))
    _check_delete_window_or_409(s.timestamp)
    try:
        db.add(
            SaidaHistorico(
                id_saida=s.id_saida,
                evento="removido_sem_inicio",
                status_anterior=s.status,
                status_novo=s.status,
                motoboy_id_anterior=s.motoboy_id,
                motoboy_id_novo=None,
                user_id=user.id,
            )
        )
        s.motoboy_id = None
        if normalizar_status_saida(s.status) in (STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA):
            s.status = STATUS_SAIU_PARA_ENTREGA
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Erro ao deletar saída.")
    return


# ============================================================
# POST /mobile/entrega/{id}/desatribuir
# ============================================================
@router.post("/entrega/{id_saida}/desatribuir")
def desatribuir_entrega(
    id_saida: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Remove atribuição: motoboy_id = null. Apenas para entregas do próprio motoboy."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    status_norm = normalizar_status_saida(s.status)
    if _status_esta_finalizado(status_norm):
        raise HTTPException(status_code=422, detail=_status_finalizado_detail(s, status_norm))
    db.add(
        SaidaHistorico(
            id_saida=s.id_saida,
            evento="desatribuido",
            status_anterior=s.status,
            status_novo=s.status,
            motoboy_id_anterior=s.motoboy_id,
            motoboy_id_novo=None,
            user_id=user.id,
        )
    )
    s.motoboy_id = None
    db.commit()
    return {"ok": True, "id_saida": id_saida}


# ============================================================
# POST /mobile/entrega/{id}/assumir
# ============================================================
@router.post("/entrega/{id_saida}/assumir")
def assumir_entrega(
    id_saida: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Reatribui a entrega para o motoboy logado (após conflito no scan). Não permite se cancelado/entregue."""
    s = db.get(Saida, id_saida)
    if not s or s.sub_base != user.sub_base:
        raise HTTPException(status_code=404, detail="Entrega não encontrada.")
    status_norm = normalizar_status_saida(s.status)
    if _status_esta_finalizado(status_norm):
        raise HTTPException(status_code=422, detail=_status_finalizado_detail(s, status_norm))
    owner_valor = _owner_valor_por_sub_base(db, user, user.sub_base or "")
    if s.motoboy_id == user.motoboy_id:
        _garantir_cobranca_owner_saida(db, s, owner_valor)
        db.commit()
        return {"ok": True, "id_saida": id_saida}

    antigo = s.motoboy_id
    s.motoboy_id = user.motoboy_id
    motoboy = db.get(Motoboy, user.motoboy_id)
    if motoboy:
        s.entregador = _get_motoboy_nome(db, motoboy)
        s.entregador_id = None
    s.status = STATUS_SAIU_PARA_ENTREGA
    if status_norm == STATUS_AUSENTE:
        detail = _get_detail_for_saida(db, id_saida)
        if detail:
            detail.tentativa = (detail.tentativa or 1) + 1
        else:
            db.add(
                SaidaDetail(
                    id_saida=id_saida,
                    id_entregador=user.motoboy_id,
                    status=STATUS_SAIU_PARA_ENTREGA,
                    tentativa=2,
                )
            )
    db.add(
        SaidaHistorico(
            id_saida=id_saida,
            evento="reatribuicao",
            status_anterior=status_norm,
            status_novo=STATUS_SAIU_PARA_ENTREGA,
            motoboy_id_anterior=antigo,
            motoboy_id_novo=user.motoboy_id,
            user_id=user.id,
        )
    )
    _garantir_cobranca_owner_saida(db, s, owner_valor)
    db.commit()
    if antigo is not None and antigo != user.motoboy_id:
        registrar_log_leitura_critico(
            sub_base=getattr(user, "sub_base", None),
            username=getattr(user, "username", None),
            origem="manual",
            tipo="saida",
            codigo=s.codigo,
            resultado="assumiu_de_outro",
            role=getattr(user, "role", None),
            motoboy_id=getattr(user, "motoboy_id", None),
            id_saida=s.id_saida,
            origem_app="mobile",
            endpoint="/mobile/entrega/{id_saida}/assumir",
        )
    return {"ok": True, "id_saida": id_saida}
