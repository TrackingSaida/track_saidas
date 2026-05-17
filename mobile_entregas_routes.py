"""
Rotas do App Motoboy (mobile).
Prefixo: /mobile
Requer JWT de motoboy (role=4, motoboy_id no token).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Tuple

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, exists
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from geocode_utils import geocode_address_any, geocode_address_with_fallbacks
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
)
from codigo_normalizer import (
    normalize_codigo,
    canonicalize_servico,
    is_qr_like_scan_payload,
)
from entregador_routes import resolver_precos_motoboy
from saida_operacional_utils import carregar_contexto_operacional

router = APIRouter(prefix="/mobile", tags=["Mobile - Entregas"])


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
    contato: Optional[str] = None
    data: Optional[date] = None
    data_hora_entrega: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    endereco_formatado: Optional[str] = None
    endereco_origem: Optional[str] = None  # manual | ocr | voz
    possui_endereco: bool = False
    tentativa: Optional[int] = None  # 1 = primeira; >= 2 exibe "Xª tentativa"


class ScanBody(BaseModel):
    codigo: str = Field(min_length=1)
    origem: str = "camera"  # camera | manual


class AusenteBody(BaseModel):
    motivo_id: int
    observacao: Optional[str] = None


class EntregueBody(BaseModel):
    tipo_recebedor: Optional[str] = None
    nome_recebedor: Optional[str] = None
    tipo_documento: Optional[str] = None
    numero_documento: Optional[str] = None
    observacao_entrega: Optional[str] = None


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
    origem: str = "manual"  # manual | ocr | voz


class IniciarRotaBody(BaseModel):
    delivery_ids: Optional[List[int]] = None  # se enviado, só esses id_saida vão para EM_ROTA


class MotivoAusenciaOut(BaseModel):
    id: int
    descricao: str


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
    rows = db.execute(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida.in_(ids))
        .order_by(SaidaDetail.id_saida.asc(), SaidaDetail.id_detail.desc())
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


def _saida_to_item(s: Saida, detail: Optional[SaidaDetail]) -> dict:
    endereco = None
    if detail and (detail.dest_rua or detail.dest_numero):
        parts = [p for p in [detail.dest_rua, detail.dest_numero, detail.dest_complemento] if p]
        endereco = ", ".join(parts) if parts else None
    lat = float(detail.latitude) if detail and detail.latitude is not None else None
    lon = float(detail.longitude) if detail and detail.longitude is not None else None
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
        "contato": detail.dest_contato if detail else None,
        "data": s.data,
        "data_hora_entrega": s.data_hora_entrega,
        "latitude": lat,
        "longitude": lon,
        "endereco_formatado": (detail.endereco_formatado or "").strip() or None if detail else None,
        "endereco_origem": (detail.endereco_origem or "").strip() or None if detail else None,
        "possui_endereco": _possui_endereco(detail),
        "tentativa": (detail.tentativa if detail and getattr(detail, "tentativa", None) is not None else None) or 1,
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
        if ctx and (ctx.removido_sem_inicio_ativo or not ctx.leitura_valida):
            continue
        ts = (ctx.operacional_ts if ctx and ctx.operacional_ts else None) or s.timestamp
        if ts and ts.date() == data_ref:
            out.append(s)
    return out


# ============================================================
# GET /mobile/entregas
# ============================================================
@router.get("/entregas", response_model=List[EntregaListItem])
def listar_entregas(
    status: Optional[str] = None,
    dia: Optional[str] = None,
    data: Optional[str] = None,
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
                hoje = date.today()
        else:
            hoje = date.today()
    else:
        hoje = None

    q = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.motoboy_id == motoboy_id,
    )
    if status == "pendente":
        q = q.where(Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]))
    elif status == "finalizadas":
        q = q.where(Saida.status == STATUS_ENTREGUE)
        if hoje is not None:
            # Filtro "finalizadas hoje": pelo evento entregue no histórico com data = hoje
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
    details_map = _carregar_details_por_saida_ids(db, [s.id_saida for s in rows])
    out = []
    for s in rows:
        out.append(_saida_to_item(s, details_map.get(int(s.id_saida))))
    return out


@router.get("/entregas/extrato", response_model=ExtratoFinanceiroOut)
def extrato_financeiro_motoboy(
    data_inicio: Optional[str] = Query(None, description="YYYY-MM-DD"),
    data_fim: Optional[str] = Query(None, description="YYYY-MM-DD"),
    status_filtro: str = Query("grupo_entregue", description="grupo_entregue | todos"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    hoje = date.today()
    inicio_in = _parse_data_yyyy_mm_dd(data_inicio)
    fim_in = _parse_data_yyyy_mm_dd(data_fim)
    if inicio_in is None or fim_in is None:
        periodo_inicio, periodo_fim = _periodo_quinzena_atual(hoje)
    else:
        periodo_inicio, periodo_fim = inicio_in, fim_in
    if periodo_inicio > periodo_fim:
        raise HTTPException(status_code=400, detail="data_inicio deve ser menor ou igual a data_fim.")

    modo = (status_filtro or "grupo_entregue").strip().lower()
    if modo not in ("grupo_entregue", "todos"):
        modo = "grupo_entregue"

    q = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.motoboy_id == motoboy_id,
        Saida.codigo.isnot(None),
        Saida.timestamp >= datetime.combine(periodo_inicio, datetime.min.time()),
        Saida.timestamp < datetime.combine(periodo_fim + timedelta(days=1), datetime.min.time()),
    ).order_by(Saida.data.desc(), Saida.timestamp.desc())
    rows_all = db.scalars(q).all()
    rows_periodo = list(rows_all)
    op_ctx_map = carregar_contexto_operacional(db, [s.id_saida for s in rows_periodo])
    rows: List[Saida] = []
    for s in rows_periodo:
        ctx = op_ctx_map.get(s.id_saida)
        if ctx and (ctx.removido_sem_inicio_ativo or not ctx.leitura_valida):
            continue
        ts_op = (ctx.operacional_ts if ctx and ctx.operacional_ts else None) or s.timestamp
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
        passa_filtro = (modo == "todos") or is_grupo_entregue

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
            hoje = date.today()
    else:
        hoje = date.today()
    rows_pendentes_all = db.scalars(
        select(Saida).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]),
        )
    ).all()
    ctx_map_pendentes = carregar_contexto_operacional(db, [s.id_saida for s in rows_pendentes_all])
    rows_pendentes = [
        s
        for s in rows_pendentes_all
        if not (ctx_map_pendentes.get(s.id_saida) and (ctx_map_pendentes[s.id_saida].removido_sem_inicio_ativo or not ctx_map_pendentes[s.id_saida].leitura_valida))
    ]
    rows_pendentes_hoje = _filtrar_por_data_operacional(db, rows_pendentes, hoje)
    pendentes = len(rows_pendentes_hoje)
    # Finalizadas hoje: baseia-se no evento "entregue" do histórico para alinhar com as telas de registros.
    finalizadas_hoje = db.scalar(
        select(func.count(Saida.id_saida))
        .where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
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
            Saida.status == STATUS_SAIU_PARA_ENTREGA,
        )
    ) or 0
    ausentes = db.scalar(
        select(func.count(Saida.id_saida)).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status == STATUS_AUSENTE,
        )
    ) or 0
    # Em atraso (D+1): considera data operacional da última ação válida.
    atraso_d1 = sum(
        1
        for s in rows_pendentes
        if ((ctx_map_pendentes.get(s.id_saida).operacional_ts if ctx_map_pendentes.get(s.id_saida) else None) or s.timestamp).date() < hoje
    )

    return {
        "pendentes": pendentes,
        "finalizadas_hoje": finalizadas_hoje,
        "pode_iniciar_rota": tem_saiu_para_entrega > 0,
        "ausentes": ausentes,
        "atraso_d1": atraso_d1,
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

    hoje = date.today()
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
            hoje = date.today()
    else:
        hoje = date.today()
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
    ordem = json.loads(rota.ordem_json) if isinstance(rota.ordem_json, str) else rota.ordem_json
    if not isinstance(ordem, list):
        ordem = []
    # Não retornar rota com todas as paradas já concluídas (evita exibir rota concluída do dia anterior)
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
    return _saida_to_item(s, detail)


# ============================================================
# PUT /mobile/entrega/{id_saida}/endereco
# ============================================================
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
    if origem not in ("manual", "ocr", "voz"):
        origem = "manual"
    parts = [body.rua, body.numero, body.complemento, body.bairro, body.cidade, body.estado, body.cep]
    endereco_formatado = ", ".join(p for p in parts if p)

    lat = body.latitude
    lon = body.longitude
    if (lat is None or lon is None) and endereco_formatado.strip():
        coords = geocode_address_with_fallbacks(
            rua=body.rua,
            numero=body.numero,
            complemento=body.complemento,
            bairro=body.bairro,
            cidade=body.cidade,
            estado=body.estado,
            cep=body.cep,
            endereco_formatado=endereco_formatado,
        )
        if coords:
            lat, lon = coords
            logging.getLogger(__name__).info(
                "Geocoding: salvando lat=%s, lon=%s para id_saida=%s",
                lat, lon, id_saida,
            )
        else:
            logging.getLogger(__name__).warning(
                "Geocoding falhou após fallbacks: id_saida=%s, endereco=%s",
                id_saida,
                endereco_formatado[:80],
            )
            raise HTTPException(
                status_code=422,
                detail="Não foi possível obter a localização deste endereço. Verifique o endereço (rua, número, bairro, cidade, estado) e tente novamente.",
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
        if lat is not None:
            detail.latitude = lat
        if lon is not None:
            detail.longitude = lon
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
            latitude=lat,
            longitude=lon,
        )
        db.add(detail)
    db.commit()
    db.refresh(detail)
    return _saida_to_item(s, detail)


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
    if status_norm == STATUS_SAIU_PARA_ENTREGA:
        raise HTTPException(
            status_code=422,
            detail="Inicie a rota antes de finalizar entregas.",
        )

    if body:
        def _set_if_present(detail: SaidaDetail) -> None:
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
    return {"ok": True, "id_saida": id_saida}


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
    return {"ok": True, "id_saida": id_saida}


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

    if status_norm == STATUS_CANCELADO:
        raise HTTPException(
            status_code=422,
            detail=f"Pedido cancelado. Não é possível registrar leitura. Status: {STATUS_CANCELADO}.",
        )

    if status_norm == STATUS_ENTREGUE:
        raise HTTPException(
            status_code=422,
            detail=f"Pedido já entregue. Não é possível registrar leitura. Status: {STATUS_ENTREGUE}.",
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
            return {"ok": True, "conflito": False, "ja_existia": True, "entrega": _saida_to_item(saida, detail)}
        if saida.motoboy_id == motoboy_id:
            if qr_payload_raw and _should_store_qr_payload_raw(servico or "", qr_payload_raw):
                if not saida.qr_payload_raw or not saida.qr_payload_raw.strip():
                    saida.qr_payload_raw = qr_payload_raw.strip()
            _garantir_cobranca_owner_saida(db, saida, owner_valor)
            db.commit()
            detail = _get_detail_for_saida(db, saida.id_saida)
            return {"ok": True, "conflito": False, "ja_existia": True, "entrega": _saida_to_item(saida, detail)}
        if saida.motoboy_id is not None:
            _garantir_cobranca_owner_saida(db, saida, owner_valor)
            db.commit()
            nome_atual = _nome_motoboy_atual(db, saida) or "outro motoboy"
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
    if status_norm == STATUS_CANCELADO:
        raise HTTPException(
            status_code=422,
            detail=f"Pedido cancelado. Não é possível assumir. Status: {STATUS_CANCELADO}.",
        )
    if status_norm == STATUS_ENTREGUE:
        raise HTTPException(
            status_code=422,
            detail=f"Pedido já entregue. Não é possível assumir. Status: {STATUS_ENTREGUE}.",
        )
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
    return {"ok": True, "id_saida": id_saida}
