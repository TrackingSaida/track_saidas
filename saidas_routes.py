from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from typing import Optional, List, Dict, Any, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
from datetime import datetime, date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, func, or_, exists, text
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, Saida, Coleta, Entregador, OwnerCobrancaItem, Motoboy, MotoboySubBase, SaidaHistorico, SaidaDetail
from saida_operacional_utils import (
    carregar_contexto_operacional,
    EVENTOS_ATRIBUICAO_VALIDOS,
    filtrar_saidas_por_periodo_operacional,
    resolver_chave_acao,
    rotulo_acao_evento,
    deve_excluir_saida_operacional,
    timestamp_operacional_saida,
    SaidaOperacionalContext,
)
from codigo_normalizer import canonicalize_servico, normalize_codigo
from saida_historico_service import SaidaHistoricoItemOut, listar_historico_saida
from pedido_campos_obrigatorios_service import (
    validate_campos_obrigatorios_conclusao,
    raise_if_campos_obrigatorios_faltando,
)
from log_leitura_service import registrar_log_leitura_critico


# ============================================================
# ROTAS DE SAÍDAS
# ============================================================

router = APIRouter(prefix="/saidas", tags=["Saídas"])
pedidos_router = APIRouter(prefix="/pedidos", tags=["Pedidos"])
MAX_IDS_POR_LOTE = 5000
OPERACAO_TZ = ZoneInfo("America/Sao_Paulo")


def _hoje_operacional() -> date:
    return datetime.now(OPERACAO_TZ).date()


# ============================================================
# SCHEMAS
# ============================================================

class SaidaCreate(BaseModel):
    entregador_id: Optional[int] = None
    entregador: Optional[str] = None
    codigo: str = Field(min_length=1)
    servico: str = Field(min_length=1)
    status: Optional[str] = None
    qr_payload_raw: Optional[str] = None  # Payload bruto do QR (ML) para etiqueta reconhecível


class SaidaOut(BaseModel):
    id_saida: int
    timestamp: datetime
    data: date
    sub_base: Optional[str]
    username: Optional[str]
    entregador: Optional[str]
    motoboy_id: Optional[int] = None
    data_hora_entrega: Optional[datetime] = None
    codigo: Optional[str]
    servico: Optional[str]
    status: Optional[str]
    base: Optional[str] = None
    is_grande: bool = False
    model_config = ConfigDict(from_attributes=True)


class SaidaGridItem(BaseModel):
    id_saida: int
    timestamp: datetime
    username: Optional[str]
    entregador: Optional[str]
    motoboy_id: Optional[int] = None
    codigo: Optional[str]
    servico: Optional[str]
    status: Optional[str]
    base: Optional[str] = None
    is_grande: bool = False
    model_config = ConfigDict(from_attributes=True)


class SaidaUpdate(BaseModel):
    entregador_id: Optional[int] = None
    entregador: Optional[str] = None
    motoboy_id: Optional[int] = None
    status: Optional[str] = None
    codigo: Optional[str] = None
    servico: Optional[str] = None
    base: Optional[str] = None
    is_grande: Optional[bool] = None  # apenas admin, root ou operador (role 0,1,2) podem alterar


class SaidaLerIn(BaseModel):
    """Payload para POST /saidas/ler — leitura unificada (1 SELECT + 1 INSERT ou UPDATE)."""
    codigo: str = Field(min_length=1)
    entregador_id: Optional[int] = None
    entregador: Optional[str] = None
    motoboy_id: Optional[int] = None  # Prioridade sobre entregador_id quando preenchido
    servico: str = Field(min_length=1)
    # Quando True e código não existe: permite registrar com status "não coletado" mesmo com ignorar_coleta=False
    registrar_nao_coletado: bool = False
    qr_payload_raw: Optional[str] = None  # Payload bruto do QR (ML) para etiqueta reconhecível


class ConfirmarNovaSaidaMesmoEntregadorIn(BaseModel):
    id_saida: int
    motoboy_id: Optional[int] = None
    entregador_id: Optional[int] = None
    entregador: Optional[str] = None
    origem: str = "web"


class LancarAvulsoIn(BaseModel):
    identificacao: Optional[str] = None
    quantidade: int = Field(default=1, ge=1)
    entregador_id: Optional[int] = None
    entregador: Optional[str] = None
    motoboy_id: Optional[int] = None


class LancarAvulsoOut(BaseModel):
    quantidade_criada: int
    codigos: List[str]
    saidas: List[dict]
    mensagem: str


class SaidaDetailOut(BaseModel):
    """Campos de saidas_detail para GET /saidas/{id_saida}."""
    id_saida: int
    id_entregador: int
    status: Optional[str] = None
    tentativa: Optional[int] = None
    motivo_ocorrencia: Optional[str] = None
    observacao_ocorrencia: Optional[str] = None
    tipo_recebedor: Optional[str] = None
    nome_recebedor: Optional[str] = None
    tipo_documento: Optional[str] = None
    numero_documento: Optional[str] = None
    observacao_entrega: Optional[str] = None
    dest_nome: Optional[str] = None
    dest_rua: Optional[str] = None
    dest_numero: Optional[str] = None
    dest_complemento: Optional[str] = None
    dest_bairro: Optional[str] = None
    dest_cidade: Optional[str] = None
    dest_estado: Optional[str] = None
    dest_cep: Optional[str] = None
    dest_contato: Optional[str] = None
    endereco_formatado: Optional[str] = None
    endereco_origem: Optional[str] = None
    foto_urls: Optional[List[str]] = None
    model_config = ConfigDict(from_attributes=True)


class SaidaDetalheCompletoOut(BaseModel):
    """Resposta de GET /saidas/{id_saida}: saida + detail."""
    id_saida: int
    timestamp: datetime
    data: date
    sub_base: Optional[str] = None
    username: Optional[str] = None
    entregador: Optional[str] = None
    motoboy_id: Optional[int] = None
    data_hora_entrega: Optional[datetime] = None
    codigo: Optional[str] = None
    servico: Optional[str] = None
    status: Optional[str] = None
    base: Optional[str] = None
    is_grande: bool = False
    detail: Optional[SaidaDetailOut] = None
    model_config = ConfigDict(from_attributes=True)


# ============================================================
# HELPERS
# ============================================================

def _normalizar_nome(s: str) -> str:
    """Lower + unaccent para comparação de nome de entregador."""
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


# Status canônicos do fluxo motoboy (armazenados em maiúsculas)
STATUS_SAIU_PARA_ENTREGA = "SAIU_PARA_ENTREGA"
STATUS_EM_ROTA = "EM_ROTA"
STATUS_ENTREGUE = "ENTREGUE"
STATUS_AUSENTE = "AUSENTE"
STATUS_CANCELADO = "CANCELADO"


def normalizar_status_saida(raw: Optional[str]) -> str:
    """Status canônico. Aceita novos status (motoboy) e legado (saiu, cancelado, etc.)."""
    s = (raw or "").strip()
    if not s:
        return "saiu"
    lower = s.lower()
    # Legado
    if lower in ("saiu", "saiu para entrega", "saiu pra entrega"):
        return "saiu"
    if lower in ("cancelado", "cancelada"):
        return STATUS_CANCELADO
    if lower in ("coletado", "coletada"):
        return "coletado"
    if lower == "aguardando_coleta":
        return "aguardando_coleta"
    if lower in ("não coletado", "nao coletado", "não coletada", "nao coletada"):
        return "não coletado"
    # Novos status motoboy (aceitar em maiúsculas ou lowercase)
    if lower in ("saiu_para_entrega", "saiu para entrega"):
        return STATUS_SAIU_PARA_ENTREGA
    if lower == "em_rota":
        return STATUS_EM_ROTA
    if lower == "entregue":
        return STATUS_ENTREGUE
    if lower == "ausente":
        return STATUS_AUSENTE
    if lower == "cancelado":
        return STATUS_CANCELADO
    return s


def _status_ja_em_rota_ou_saida(status_norm: str) -> bool:
    """
    True quando o pacote já saiu / está em rota com um entregador.
    Nesses estados, leitura por outro motoboy deve retornar 409 TROCA_ENTREGADOR
    (o ramo legado só checava status_norm == 'saiu' e ignorava SAIU_PARA_ENTREGA / EM_ROTA).
    """
    if status_norm == "saiu":
        return True
    if status_norm in (STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA):
        return True
    return False


def _status_esta_finalizado(status_norm: str) -> bool:
    return status_norm in {STATUS_ENTREGUE, STATUS_CANCELADO}


def _status_finalizado_detail(saida: Saida, status_norm: Optional[str] = None) -> dict:
    status_atual = status_norm or normalizar_status_saida(saida.status)
    return {
        "code": "STATUS_FINALIZADO",
        "id_saida": saida.id_saida,
        "status_atual": status_atual,
        "message": f"Pedido com status finalizado: {status_atual}.",
        "entregador_atual": saida.entregador,
    }


def _validar_alteracao_saida_finalizada(
    obj: Saida,
    status_anterior: str,
    payload: SaidaUpdate,
) -> None:
    """Bloqueia alterações inválidas em status finalizado; permite ENTREGUE→CANCELADO e reatribuição."""
    if not _status_esta_finalizado(status_anterior):
        return
    if status_anterior == STATUS_CANCELADO:
        raise HTTPException(status_code=422, detail=_status_finalizado_detail(obj, status_anterior))
    if status_anterior == STATUS_ENTREGUE:
        cancelando = (
            payload.status is not None
            and normalizar_status_saida(payload.status) == STATUS_CANCELADO
        )
        reatribuindo = (
            payload.motoboy_id is not None
            and int(payload.motoboy_id) != int(obj.motoboy_id or 0)
        )
        if payload.status is not None and not cancelando:
            raise HTTPException(status_code=422, detail=_status_finalizado_detail(obj, status_anterior))
        if not cancelando and not reatribuindo:
            has_other = any(
                [
                    payload.codigo is not None,
                    payload.servico is not None,
                    payload.base is not None,
                    payload.is_grande is not None,
                    payload.entregador_id is not None,
                    payload.entregador is not None and (payload.entregador or "").strip(),
                    payload.motoboy_id is not None,
                ]
            )
            if has_other:
                raise HTTPException(status_code=422, detail=_status_finalizado_detail(obj, status_anterior))
        return
    raise HTTPException(status_code=422, detail=_status_finalizado_detail(obj, status_anterior))


def _aplicar_detail_reatribuicao_entregue(
    db: Session,
    id_saida: int,
    motoboy_id: int,
    status_novo: str,
) -> None:
    detail = db.scalar(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida == id_saida)
        .order_by(SaidaDetail.id_detail.desc())
        .limit(1)
    )
    if detail:
        detail.status = status_novo
        detail.id_entregador = motoboy_id
        detail.tentativa = (detail.tentativa or 1) + 1
    else:
        db.add(
            SaidaDetail(
                id_saida=id_saida,
                id_entregador=motoboy_id,
                status=status_novo,
                tentativa=2,
            )
        )


def _evento_status_manual(status_norm: str) -> Optional[str]:
    if status_norm == STATUS_CANCELADO:
        return "cancelado"
    if status_norm == STATUS_ENTREGUE:
        return "entregue"
    if status_norm == STATUS_AUSENTE:
        return "ausente"
    if status_norm in ("coletado",):
        return "status_coletado_manual"
    if status_norm in ("não coletado", "nao coletado"):
        return "status_nao_coletado_manual"
    if status_norm in ("saiu", STATUS_SAIU_PARA_ENTREGA):
        return "status_saiu_manual"
    return None


def _ctx_data_operacional(saida: Saida, ctx_map: dict[int, object]) -> date:
    ctx = ctx_map.get(int(saida.id_saida))
    op_ts = getattr(ctx, "operacional_ts", None) if ctx else None
    base_ts = op_ts or getattr(saida, "timestamp", None)
    if base_ts is not None:
        return base_ts.date()
    return getattr(saida, "data", None) or _hoje_operacional()


def _montar_payload_nova_saida_mesmo_entregador(
    *,
    data_operacional_anterior: date,
    data_operacional_nova: date,
    id_motoboy: Optional[int],
    confirmado_por: Optional[str],
    origem: str,
    data_hora_confirmacao: datetime,
) -> str:
    payload = {
        "tipo_evento": "nova_saida_mesmo_entregador",
        "data_operacional_anterior": data_operacional_anterior.isoformat(),
        "data_operacional_nova": data_operacional_nova.isoformat(),
        "id_motoboy": int(id_motoboy) if id_motoboy is not None else None,
        "confirmado_por": confirmado_por or "",
        "origem": origem or "desconhecida",
        "data_hora_confirmacao": data_hora_confirmacao.isoformat(sep=" ", timespec="seconds"),
    }
    return json.dumps(payload, ensure_ascii=False)


def _resolve_entregador(
    db: Session,
    sub_base: str,
    entregador_id: Optional[int] = None,
    entregador_nome: Optional[str] = None,
) -> tuple[int, str]:
    """
    Retorna (id_entregador, nome). Prioriza entregador_id.
    Levanta HTTPException 422 se não encontrar ou nenhum dado enviado.
    """
    if entregador_id is not None:
        ent = db.get(Entregador, entregador_id)
        if not ent or ent.sub_base != sub_base:
            raise HTTPException(
                status_code=422,
                detail={"code": "ENTREGADOR_INVALIDO", "message": "Entregador não encontrado ou não pertence à sua base."},
            )
        return ent.id_entregador, (ent.nome or "").strip()

    if entregador_nome and entregador_nome.strip():
        nome_busca = _normalizar_nome(entregador_nome)
        ent = db.scalar(
            select(Entregador).where(
                Entregador.sub_base == sub_base,
                func.lower(func.unaccent(Entregador.nome)) == nome_busca,
            )
        )
        if ent:
            return ent.id_entregador, (ent.nome or "").strip()
        raise HTTPException(
            status_code=422,
            detail={"code": "ENTREGADOR_NAO_ENCONTRADO", "message": "Entregador não encontrado pelo nome."},
        )

    raise HTTPException(
        status_code=422,
        detail={"code": "ENTREGADOR_OBRIGATORIO", "message": "Informe entregador_id ou entregador (nome)."},
    )


def _get_motoboy_nome(db: Session, motoboy: Motoboy) -> str:
    """Retorna nome do motoboy (User) para exibição."""
    if not motoboy or not motoboy.user_id:
        return "Motoboy"
    u = db.get(User, motoboy.user_id)
    if not u:
        return "Motoboy"
    nome = f"{u.nome or ''} {u.sobrenome or ''}".strip() or u.username or ""
    return nome or f"Motoboy {motoboy.id_motoboy}"


def _resolve_motoboy_for_subbase(db: Session, sub_base: str, motoboy_id: int) -> Motoboy:
    """Retorna o Motoboy se existir e estiver vinculado à sub_base. Levanta 422 caso contrário."""
    motoboy = db.get(Motoboy, motoboy_id)
    if not motoboy:
        raise HTTPException(
            status_code=422,
            detail={"code": "MOTOBOY_NAO_ENCONTRADO", "message": "Motoboy não encontrado."},
        )
    vinculado = db.scalar(
        select(MotoboySubBase).where(
            MotoboySubBase.motoboy_id == motoboy_id,
            MotoboySubBase.sub_base == sub_base,
            MotoboySubBase.ativo.is_(True),
        )
    )
    if not vinculado:
        raise HTTPException(
            status_code=422,
            detail={"code": "MOTOBOY_NAO_VINCULADO", "message": "Motoboy não vinculado a esta sub_base."},
        )
    return motoboy


def _get_owned_saida(db: Session, sub_base: str, id_saida: int) -> Saida:
    obj = db.get(Saida, id_saida)
    if not obj or obj.sub_base != sub_base:
        raise HTTPException(
            status_code=404,
            detail={"code": "SAIDA_NOT_FOUND", "message": "Saída não encontrada."}
        )
    return obj


def _check_delete_window_or_409(ts: datetime):
    if ts is None or datetime.utcnow() - ts > timedelta(days=1):
        raise HTTPException(
            409,
            {"code": "DELETE_WINDOW_EXPIRED", "message": "Janela para exclusão expirada."}
        )


def _should_store_qr_payload_raw(servico: str, qr_raw: Optional[str]) -> bool:
    """Armazena qr_payload_raw somente para Mercado Livre com formato válido."""
    if not qr_raw or not qr_raw.strip():
        return False
    s = servico.strip().lower()
    if "mercado" not in s and "ml" not in s and "flex" not in s:
        return False
    raw = qr_raw.strip()
    # JSON com id, sender_id, hash_code
    if raw.startswith("{") and ("sender_id" in raw or "SENDER_ID" in raw or "hash_code" in raw):
        return True
    # Formato antigo com dígitos ML (4[5-9]...)
    if re.search(r"4[5-9]\d{9}", raw):
        return True
    return False


def _normalizar_label_avulso(label: Optional[str]) -> str:
    raw = unicodedata.normalize("NFD", (label or "").strip().upper())
    ascii_only = "".join(c for c in raw if unicodedata.category(c) != "Mn")
    ascii_only = re.sub(r"[^A-Z0-9]+", "-", ascii_only).strip("-")
    ascii_only = re.sub(r"-{2,}", "-", ascii_only)
    return ascii_only[:48]


def _next_avulso_seq(db: Session) -> int:
    db.execute(text("CREATE SEQUENCE IF NOT EXISTS avulso_codigo_seq START WITH 1 INCREMENT BY 1"))
    return int(db.execute(text("SELECT nextval('avulso_codigo_seq')")).scalar_one())


def _gerar_codigo_avulso(db: Session, label_norm: str) -> str:
    while True:
        seq = _next_avulso_seq(db)
        sufixo = f"{seq:06d}"
        codigo = f"AVULSO-{label_norm}-{sufixo}" if label_norm else f"AVULSO-{sufixo}"
        # Checagem global (sem sub_base): avulso_codigo_seq é cluster-wide; garante unicidade do código gerado.
        # TODO: remover SELECT defensivo e confiar em sequence + constraint UNIQUE quando existir.
        existe = db.scalar(select(Saida.id_saida).where(Saida.codigo == codigo).limit(1))
        if not existe:
            return codigo


def _servico_text_expr(expr):
    """Normaliza expressão SQL de serviço para comparações semânticas."""
    return func.coalesce(func.unaccent(func.lower(expr)), "")


def _servico_is_shopee_expr(expr):
    return _servico_text_expr(expr).like("%shopee%")


def _servico_is_mercado_expr(expr):
    srv = _servico_text_expr(expr)
    return srv.like("%mercado%") | srv.like("%flex%") | srv.like("%ml%")


def _norm_text(value: Optional[str]) -> str:
    return unicodedata.normalize("NFD", (value or "").strip().lower()).encode("ascii", "ignore").decode("ascii")


def _parse_multi_values(values: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    for raw in values or []:
        if raw is None:
            continue
        for part in str(raw).split(","):
            token = part.strip()
            if token:
                out.append(token)
    return out


def _chunked_ids(values: List[int], chunk_size: int = MAX_IDS_POR_LOTE) -> List[List[int]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size deve ser maior que zero")
    return [values[i : i + chunk_size] for i in range(0, len(values), chunk_size)]


def _status_group_aliases(token: str) -> List[str]:
    key = _norm_text(token).replace("_", " ").replace("-", " ")
    key = " ".join(key.split())
    groups = {
        "saiu": ["saiu", "saiu para entrega", "saiu pra entrega", "saiu_pra_entrega", "saiu_para_entrega"],
        "saiu para entrega": ["saiu", "saiu para entrega", "saiu pra entrega", "saiu_pra_entrega", "saiu_para_entrega"],
        "em rota": ["em rota", "em_rota"],
        "entregue": ["entregue"],
        "ausente": ["ausente"],
        "coletado": ["coletado"],
        "nao coletado": ["nao coletado", "não coletado"],
        "cancelado": ["cancelado", "cancelados"],
    }
    normalized = groups.get(key, [key])
    return sorted({v for v in normalized if v})


def _acao_equivalente(evento_norm: str) -> str:
    return resolver_chave_acao(evento_norm) or ""


def _nome_executor_atual(db: Session, saida: Saida) -> Optional[str]:
    """Resolve nome exibível do responsável atual priorizando motoboy_id."""
    mid = getattr(saida, "motoboy_id", None)
    if mid is not None:
        motoboy = db.get(Motoboy, mid)
        if motoboy:
            return _get_motoboy_nome(db, motoboy)
    return getattr(saida, "entregador", None)


# ============================================================
# POST — REGISTRAR SAÍDA
# ============================================================

@router.post("/registrar", status_code=201)
def registrar_saida(
    payload: SaidaCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Dados vindos do JWT
    sub_base = current_user.sub_base
    username = current_user.username
    ignorar_coleta = bool(current_user.ignorar_coleta)
    owner_valor = Decimal(getattr(current_user, "owner_valor", 0))

    if not sub_base or not username:
        raise HTTPException(401, "Usuário inválido.")

    # Resolver entregador (obrigatório): prioriza entregador_id, depois nome
    entregador_id, entregador_nome = _resolve_entregador(
        db, sub_base,
        entregador_id=payload.entregador_id,
        entregador_nome=payload.entregador,
    )

    # Normalização
    codigo = payload.codigo.strip()
    servico = canonicalize_servico(payload.servico)
    status_val = normalizar_status_saida(payload.status)

    # Duplicidade
    existente = db.scalar(
        select(Saida.id_saida).where(
            Saida.sub_base == sub_base,
            Saida.codigo == codigo
        )
    )
    if existente:
        raise HTTPException(
            409,
            {"code": "DUPLICATE_SAIDA", "message": f"Código '{codigo}' já registrado."}
        )

    # Coleta obrigatória
    if not ignorar_coleta:
        coleta_exists = db.scalar(
            select(Coleta.id_coleta).where(
                Coleta.sub_base == sub_base,
                Coleta.username_entregador == entregador_nome
            )
        )
        if not coleta_exists:
            raise HTTPException(
                409,
                {"code": "COLETA_OBRIGATORIA", "message": "Este cliente exige coleta antes da saída."}
            )

    qr_raw = getattr(payload, "qr_payload_raw", None)
    store_qr = _should_store_qr_payload_raw(servico, qr_raw)

    try:
        row = Saida(
            sub_base=sub_base,
            username=username,
            entregador=entregador_nome,
            entregador_id=entregador_id,
            codigo=codigo,
            servico=servico,
            status=status_val,
            qr_payload_raw=qr_raw.strip() if store_qr and qr_raw else None,
        )
        db.add(row)
        db.flush()

        # ignorar_coleta: cobrança apenas para status "saiu" (cancelado e outros não cobram)
        if ignorar_coleta and status_val == "saiu":
            db.add(
                OwnerCobrancaItem(
                    sub_base=sub_base,
                    id_coleta=None,
                    id_saida=row.id_saida,
                    valor=owner_valor,
                )
            )

        db.commit()
        db.refresh(row)

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Erro ao registrar saída: {e}")

    return SaidaOut.model_validate(row)


# ============================================================
# POST — LER SAÍDA (fluxo unificado: 1 SELECT + 1 INSERT ou UPDATE)
# ============================================================
# Performance: evita GET listar?codigo= pesado; um único request decide e persiste.
# Idempotência: mesmo código + mesmo entregador → 200 com dados existentes (409 só para TROCA_ENTREGADOR).
@router.post("/ler")
def ler_saida(
    payload: SaidaLerIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = current_user.sub_base
    username = current_user.username
    role = getattr(current_user, "role", None)
    ignorar_coleta = bool(current_user.ignorar_coleta)
    owner_valor = Decimal(getattr(current_user, "owner_valor", 0))

    if not sub_base or not username:
        raise HTTPException(401, "Usuário inválido.")

    motoboy_id: Optional[int] = None
    entregador_id: Optional[int] = None
    entregador_nome: str = ""

    if payload.motoboy_id is not None:
        motoboy = _resolve_motoboy_for_subbase(db, sub_base, payload.motoboy_id)
        motoboy_id = motoboy.id_motoboy
        entregador_nome = _get_motoboy_nome(db, motoboy)
    elif payload.entregador_id is not None or (payload.entregador and payload.entregador.strip()):
        entregador_id, entregador_nome = _resolve_entregador(
            db, sub_base,
            entregador_id=payload.entregador_id,
            entregador_nome=payload.entregador,
        )
    else:
        raise HTTPException(
            status_code=422,
            detail={"code": "ENTREGADOR_OBRIGATORIO", "message": "Informe motoboy_id ou entregador_id/entregador."},
        )

    codigo_norm, servico_norm, qr_from_norm = normalize_codigo(payload.codigo.strip(), strict_qr=False)
    if codigo_norm is None or servico_norm is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CODIGO_INVALIDO",
                "message": "Código inválido. Use etiqueta Shopee, Mercado Livre, telefone válido ou AVULSO-*.",
            },
        )
    codigo = codigo_norm
    servico = canonicalize_servico(servico_norm)
    qr_payload_raw = payload.qr_payload_raw
    if qr_from_norm and not qr_payload_raw:
        qr_payload_raw = qr_from_norm

    # 1 SELECT por (sub_base, codigo) — índice existente, O(1)
    existente = db.scalar(
        select(Saida).where(
            Saida.sub_base == sub_base,
            Saida.codigo == codigo,
        )
    )

    if existente is None:
        # Não existe: ignorar_coleta ou registrar_nao_coletado → INSERT; senão 422 (erro de negócio, sem retry)
        if not ignorar_coleta and not payload.registrar_nao_coletado:
            # JSONResponse direto para preservar código de erro sem passar pelo handler global.
            return JSONResponse(
                status_code=422,
                content={"code": "NAO_COLETADO", "message": "Código não coletado."},
            )
        # status: "saiu" quando ignorar_coleta; "não coletado" quando usuário confirmou registrar mesmo assim
        status_inicial = "não coletado" if payload.registrar_nao_coletado else (
            STATUS_SAIU_PARA_ENTREGA if motoboy_id else "saiu"
        )
        store_qr = _should_store_qr_payload_raw(servico, qr_payload_raw)
        try:
            row = Saida(
                sub_base=sub_base,
                username=username,
                entregador=entregador_nome,
                entregador_id=entregador_id,
                motoboy_id=motoboy_id,
                codigo=codigo,
                servico=servico,
                status=status_inicial,
                qr_payload_raw=qr_payload_raw.strip() if store_qr and qr_payload_raw else None,
            )
            db.add(row)
            db.flush()
            db.add(
                SaidaHistorico(
                    id_saida=row.id_saida,
                    evento="lido",
                    status_novo=status_inicial,
                    user_id=getattr(current_user, "id", None),
                )
            )
            db.add(
                OwnerCobrancaItem(
                    sub_base=sub_base,
                    id_coleta=None,
                    id_saida=row.id_saida,
                    valor=owner_valor,
                )
            )
            db.commit()
            db.refresh(row)
            return JSONResponse(
                status_code=201,
                content=SaidaOut.model_validate(row).model_dump(mode="json"),
            )
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(500, f"Erro ao registrar saída: {e}")

    # Existe: decidir por status e entregador
    status_norm = normalizar_status_saida(existente.status)

    if status_norm == "aguardando_coleta":
        # Leitura em coleta: aguardando_coleta → coletado
        status_anterior = existente.status
        existente.status = "coletado"
        existente.entregador_id = entregador_id
        existente.entregador = entregador_nome
        if motoboy_id is not None:
            existente.motoboy_id = motoboy_id
        detail = db.scalar(
            select(SaidaDetail).where(SaidaDetail.id_saida == existente.id_saida).order_by(SaidaDetail.id_detail.desc()).limit(1)
        )
        if detail:
            detail.status = "Coletado"
            if entregador_id is not None:
                detail.id_entregador = entregador_id
            elif motoboy_id is not None:
                detail.id_entregador = motoboy_id
        db.add(
            SaidaHistorico(
                id_saida=existente.id_saida,
                evento="lido",
                status_anterior=status_anterior,
                status_novo="coletado",
                user_id=getattr(current_user, "id", None),
            )
        )
        try:
            db.commit()
            db.refresh(existente)
            return SaidaOut.model_validate(existente)
        except Exception:
            db.rollback()
            raise HTTPException(500, "Erro ao atualizar saída.")

    if status_norm == "coletado":
        # coletado → UPDATE para saiu / SAIU_PARA_ENTREGA
        status_anterior = existente.status
        existente.status = STATUS_SAIU_PARA_ENTREGA if motoboy_id else "saiu"
        existente.entregador_id = entregador_id
        existente.entregador = entregador_nome
        if motoboy_id is not None:
            existente.motoboy_id = motoboy_id
        db.add(
            SaidaHistorico(
                id_saida=existente.id_saida,
                evento="lido",
                status_anterior=status_anterior,
                status_novo=existente.status,
                user_id=getattr(current_user, "id", None),
            )
        )
        try:
            db.commit()
            db.refresh(existente)
            return SaidaOut.model_validate(existente)
        except Exception:
            db.rollback()
            raise HTTPException(500, "Erro ao atualizar saída.")

    if _status_ja_em_rota_ou_saida(status_norm):
        # mesmo entregador/motoboy → 200 idempotente (sem 409)
        mesmo_ent = False
        if motoboy_id is not None:
            mesmo_ent = existente.motoboy_id == motoboy_id
        if not mesmo_ent and entregador_id is not None:
            mesmo_ent = existente.entregador_id == entregador_id
        if not mesmo_ent:
            mesmo_ent = _normalizar_nome(entregador_nome or "") == _normalizar_nome(existente.entregador or "")
        if mesmo_ent:
            if _status_esta_finalizado(status_norm):
                registrar_log_leitura_critico(
                    sub_base=sub_base,
                    username=username,
                    origem="desconhecida",
                    tipo="saida",
                    codigo=existente.codigo,
                    resultado="bloqueio_status_finalizado",
                    role=role,
                    motoboy_id=motoboy_id,
                    id_saida=existente.id_saida,
                    origem_app="web",
                    endpoint="/saidas/ler",
                )
                return JSONResponse(
                    status_code=422,
                    content={
                        "code": "STATUS_FINALIZADO",
                        "id_saida": existente.id_saida,
                        "status_atual": status_norm,
                        "message": f"Pedido com status finalizado: {status_norm}.",
                        "entregador_atual": existente.entregador,
                    },
                )
            ctx_map = carregar_contexto_operacional(db, [existente.id_saida])
            data_operacional = _ctx_data_operacional(existente, ctx_map)
            hoje = _hoje_operacional()
            if data_operacional < hoje:
                registrar_log_leitura_critico(
                    sub_base=sub_base,
                    username=username,
                    origem="desconhecida",
                    tipo="saida",
                    codigo=existente.codigo,
                    resultado="leitura_dia_anterior_aguardando_confirmacao",
                    role=role,
                    motoboy_id=motoboy_id,
                    id_saida=existente.id_saida,
                    origem_app="web",
                    endpoint="/saidas/ler",
                )
                return JSONResponse(
                    status_code=409,
                    content={
                        "code": "LEITURA_DIA_ANTERIOR",
                        "id_saida": existente.id_saida,
                        "data_operacional_anterior": data_operacional.isoformat(),
                        "status_atual": existente.status,
                        "motoboy_id": existente.motoboy_id,
                        "motoboy_nome": existente.entregador,
                        "message": "Pedido já lido em data anterior para o mesmo motoboy.",
                    },
                )
            registrar_log_leitura_critico(
                sub_base=sub_base,
                username=username,
                origem="desconhecida",
                tipo="saida",
                codigo=existente.codigo,
                resultado="duplicado",
                role=role,
                motoboy_id=motoboy_id,
                id_saida=existente.id_saida,
                origem_app="web",
                endpoint="/saidas/ler",
            )
            return SaidaOut.model_validate(existente)
        # outro entregador → 409 para front acionar PATCH de troca.
        # Sem retry: front trata com Swal + PATCH, evita latência de retry em fluxo normal.
        registrar_log_leitura_critico(
            sub_base=sub_base,
            username=username,
            origem="desconhecida",
            tipo="saida",
            codigo=existente.codigo,
            resultado="atribuido_a_outro",
            role=role,
            motoboy_id=motoboy_id,
            id_saida=existente.id_saida,
            origem_app="web",
            endpoint="/saidas/ler",
        )
        troca_payload = {
            "code": "TROCA_ENTREGADOR",
            "id_saida": existente.id_saida,
            "message": "Código já saiu com outro entregador.",
            "entregador_atual": existente.entregador,
            "username": existente.username,
            "status_atual": status_norm,
        }
        return JSONResponse(status_code=409, content=troca_payload)

    if status_norm == STATUS_ENTREGUE:
        mesmo_ent = False
        if motoboy_id is not None:
            mesmo_ent = existente.motoboy_id == motoboy_id
        if not mesmo_ent and entregador_id is not None:
            mesmo_ent = existente.entregador_id == entregador_id
        if not mesmo_ent:
            mesmo_ent = _normalizar_nome(entregador_nome or "") == _normalizar_nome(existente.entregador or "")
        if not mesmo_ent:
            registrar_log_leitura_critico(
                sub_base=sub_base,
                username=username,
                origem="desconhecida",
                tipo="saida",
                codigo=existente.codigo,
                resultado="atribuido_a_outro",
                role=role,
                motoboy_id=motoboy_id,
                id_saida=existente.id_saida,
                origem_app="web",
                endpoint="/saidas/ler",
            )
            return JSONResponse(
                status_code=409,
                content={
                    "code": "TROCA_ENTREGADOR",
                    "id_saida": existente.id_saida,
                    "message": "Pedido entregue com outro entregador.",
                    "entregador_atual": existente.entregador,
                    "username": existente.username,
                    "status_atual": status_norm,
                },
            )
        registrar_log_leitura_critico(
            sub_base=sub_base,
            username=username,
            origem="desconhecida",
            tipo="saida",
            codigo=existente.codigo,
            resultado="bloqueio_status_finalizado",
            role=role,
            motoboy_id=motoboy_id,
            id_saida=existente.id_saida,
            origem_app="web",
            endpoint="/saidas/ler",
        )
        return JSONResponse(
            status_code=422,
            content=_status_finalizado_detail(existente, status_norm),
        )

    if _status_esta_finalizado(status_norm):
        registrar_log_leitura_critico(
            sub_base=sub_base,
            username=username,
            origem="desconhecida",
            tipo="saida",
            codigo=existente.codigo,
            resultado="bloqueio_status_finalizado",
            role=role,
            motoboy_id=motoboy_id,
            id_saida=existente.id_saida,
            origem_app="web",
            endpoint="/saidas/ler",
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": "STATUS_FINALIZADO",
                "id_saida": existente.id_saida,
                "status_atual": status_norm,
                "message": f"Pedido com status finalizado: {status_norm}.",
                "entregador_atual": existente.entregador,
            },
        )

    # status cancelado ou outro: retornar como está (idempotente) ou 422 conforme regra de negócio
    return SaidaOut.model_validate(existente)


@router.post("/confirmar-nova-saida-mesmo-entregador")
def confirmar_nova_saida_mesmo_entregador(
    payload: ConfirmarNovaSaidaMesmoEntregadorIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = current_user.sub_base
    username = current_user.username
    if not sub_base or not username:
        raise HTTPException(401, "Usuário inválido.")

    saida = db.scalar(
        select(Saida).where(
            Saida.id_saida == payload.id_saida,
            Saida.sub_base == sub_base,
        )
    )
    if saida is None:
        raise HTTPException(404, "Saída não encontrada.")

    status_norm = normalizar_status_saida(saida.status)
    if _status_esta_finalizado(status_norm):
        registrar_log_leitura_critico(
            sub_base=sub_base,
            username=username,
            origem="desconhecida",
            tipo="saida",
            codigo=saida.codigo,
            resultado="bloqueio_status_finalizado",
            role=getattr(current_user, "role", None),
            motoboy_id=saida.motoboy_id,
            id_saida=saida.id_saida,
            origem_app=payload.origem or "web",
            endpoint="/saidas/confirmar-nova-saida-mesmo-entregador",
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": "STATUS_FINALIZADO",
                "id_saida": saida.id_saida,
                "status_atual": status_norm,
                "message": f"Pedido com status finalizado: {status_norm}.",
                "entregador_atual": saida.entregador,
            },
        )

    mesmo_ent = False
    if payload.motoboy_id is not None:
        mesmo_ent = saida.motoboy_id == payload.motoboy_id
    if not mesmo_ent and payload.entregador_id is not None:
        mesmo_ent = saida.entregador_id == payload.entregador_id
    if not mesmo_ent and payload.entregador:
        mesmo_ent = _normalizar_nome(payload.entregador) == _normalizar_nome(saida.entregador or "")
    if not mesmo_ent:
        return JSONResponse(
            status_code=409,
            content={
                "code": "TROCA_ENTREGADOR",
                "id_saida": saida.id_saida,
                "message": "Código já saiu com outro entregador.",
                "entregador_atual": saida.entregador,
                "username": saida.username,
            },
        )

    ctx_map = carregar_contexto_operacional(db, [saida.id_saida])
    data_operacional_anterior = _ctx_data_operacional(saida, ctx_map)
    hoje = _hoje_operacional()
    if data_operacional_anterior >= hoje:
        registrar_log_leitura_critico(
            sub_base=sub_base,
            username=username,
            origem="desconhecida",
            tipo="saida",
            codigo=saida.codigo,
            resultado="duplicado",
            role=getattr(current_user, "role", None),
            motoboy_id=saida.motoboy_id,
            id_saida=saida.id_saida,
            origem_app=payload.origem or "web",
            endpoint="/saidas/confirmar-nova-saida-mesmo-entregador",
        )
        return SaidaOut.model_validate(saida)

    data_hora_confirmacao = datetime.now()
    payload_historico = _montar_payload_nova_saida_mesmo_entregador(
        data_operacional_anterior=data_operacional_anterior,
        data_operacional_nova=hoje,
        id_motoboy=saida.motoboy_id,
        confirmado_por=username,
        origem=payload.origem or "desconhecida",
        data_hora_confirmacao=data_hora_confirmacao,
    )
    db.add(
        SaidaHistorico(
            id_saida=saida.id_saida,
            evento="nova_saida_mesmo_entregador",
            status_anterior=saida.status,
            status_novo=saida.status,
            motoboy_id_anterior=saida.motoboy_id,
            motoboy_id_novo=saida.motoboy_id,
            user_id=getattr(current_user, "id", None),
            payload=payload_historico,
        )
    )
    try:
        db.commit()
        db.refresh(saida)
    except Exception:
        db.rollback()
        raise HTTPException(500, "Erro ao confirmar nova saída para o mesmo motoboy.")

    registrar_log_leitura_critico(
        sub_base=sub_base,
        username=username,
        origem="desconhecida",
        tipo="saida",
        codigo=saida.codigo,
        resultado="nova_saida_mesmo_entregador_confirmada",
        role=getattr(current_user, "role", None),
        motoboy_id=saida.motoboy_id,
        id_saida=saida.id_saida,
        origem_app=payload.origem or "web",
        endpoint="/saidas/confirmar-nova-saida-mesmo-entregador",
    )
    return SaidaOut.model_validate(saida)


def _lancar_avulso_impl(
    payload: LancarAvulsoIn,
    db: Session,
    current_user: User,
):
    sub_base = current_user.sub_base
    username = current_user.username
    if not sub_base or not username:
        raise HTTPException(401, "Usuário inválido.")

    owner_valor = Decimal(getattr(current_user, "owner_valor", 0))
    role = int(getattr(current_user, "role", 0) or 0)

    motoboy_id: Optional[int] = None
    entregador_id: Optional[int] = None
    entregador_nome = ""

    if payload.motoboy_id is not None:
        motoboy = _resolve_motoboy_for_subbase(db, sub_base, payload.motoboy_id)
        motoboy_id = motoboy.id_motoboy
        entregador_nome = _get_motoboy_nome(db, motoboy)
    elif payload.entregador_id is not None or (payload.entregador and payload.entregador.strip()):
        entregador_id, entregador_nome = _resolve_entregador(
            db,
            sub_base,
            entregador_id=payload.entregador_id,
            entregador_nome=payload.entregador,
        )
    elif role == 4 and getattr(current_user, "motoboy_id", None):
        motoboy = _resolve_motoboy_for_subbase(db, sub_base, int(current_user.motoboy_id))
        motoboy_id = motoboy.id_motoboy
        entregador_nome = _get_motoboy_nome(db, motoboy)
    else:
        raise HTTPException(
            status_code=422,
            detail={"code": "ENTREGADOR_OBRIGATORIO", "message": "Informe motoboy_id ou entregador_id/entregador."},
        )

    quantidade = int(payload.quantidade or 0)
    if quantidade < 1:
        raise HTTPException(
            status_code=422,
            detail={"code": "QUANTIDADE_INVALIDA", "message": "Quantidade mínima é 1."},
        )

    label_norm = _normalizar_label_avulso(payload.identificacao)
    codigos: List[str] = []
    saidas_criadas: List[dict] = []
    status_inicial = STATUS_SAIU_PARA_ENTREGA if motoboy_id else "saiu"
    servico = canonicalize_servico("Avulso")

    try:
        for _ in range(quantidade):
            codigo = _gerar_codigo_avulso(db, label_norm)
            row = Saida(
                sub_base=sub_base,
                username=username,
                entregador=entregador_nome,
                entregador_id=entregador_id,
                motoboy_id=motoboy_id,
                codigo=codigo,
                servico=servico,
                status=status_inicial,
                base=(payload.identificacao or "").strip() or None,
            )
            db.add(row)
            db.flush()
            db.add(
                SaidaHistorico(
                    id_saida=row.id_saida,
                    evento="lancar_avulso",
                    status_novo=status_inicial,
                    user_id=getattr(current_user, "id", None),
                    payload=json.dumps(
                        {
                            "identificacao": (payload.identificacao or "").strip() or None,
                            "codigo": codigo,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            db.add(
                OwnerCobrancaItem(
                    sub_base=sub_base,
                    id_coleta=None,
                    id_saida=row.id_saida,
                    valor=owner_valor,
                )
            )
            codigos.append(codigo)
            saidas_criadas.append(
                {
                    "id_saida": int(row.id_saida),
                    "codigo": codigo,
                    "servico": servico,
                    "status": status_inicial,
                }
            )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Erro ao lançar avulso: {e}")

    qtd = len(codigos)
    msg = "1 avulso lançado com sucesso." if qtd == 1 else f"{qtd} avulsos lançados com sucesso."
    return {"quantidade_criada": qtd, "codigos": codigos, "saidas": saidas_criadas, "mensagem": msg}


@router.post("/pedidos/lancar-avulso", response_model=LancarAvulsoOut)
def lancar_avulso_legacy(
    payload: LancarAvulsoIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _lancar_avulso_impl(payload, db, current_user)


@pedidos_router.post("/lancar-avulso", response_model=LancarAvulsoOut)
def lancar_avulso(
    payload: LancarAvulsoIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _lancar_avulso_impl(payload, db, current_user)


# ============================================================
# LISTAR — helpers (fast path por código exato + item JSON)
# ============================================================

def _listar_resposta_vazia() -> Dict[str, Any]:
    return {"total": 0, "sumShopee": 0, "sumMercado": 0, "sumAvulso": 0, "items": []}


def _normalizar_codigo_busca_listar(codigo: str) -> str:
    codigo_norm, _, _ = normalize_codigo(codigo.strip(), strict_qr=False)
    if codigo_norm is None:
        codigo_norm = codigo.strip().upper()
    return codigo_norm


def _buscar_saida_codigo_exato(db: Session, sub_base: str, codigo_norm: str) -> Optional[Saida]:
    row = db.scalar(
        select(Saida)
        .where(
            Saida.sub_base == sub_base,
            Saida.codigo == codigo_norm,
        )
        .limit(1)
    )
    if row is not None:
        return row
    codigo_upper = codigo_norm.upper()
    return db.scalar(
        select(Saida)
        .where(
            Saida.sub_base == sub_base,
            func.upper(Saida.codigo) == codigo_upper,
        )
        .limit(1)
    )


def _contar_servico_listar(servico: Optional[str]) -> Tuple[int, int, int]:
    srv = (servico or "").strip().lower()
    if ("shopee" in srv) or ("spx" in srv):
        return 1, 0, 0
    if (
        ("mercado livre" in srv)
        or ("mercado_livre" in srv)
        or ("mercadolivre" in srv)
        or (" ml" in f" {srv}")
        or ("flex" in srv)
    ):
        return 0, 1, 0
    return 0, 0, 1


def _montar_item_listar_saida(
    row: Saida,
    op_ctx: Optional[SaidaOperacionalContext],
    nome_executor: Optional[str],
) -> Dict[str, Any]:
    return {
        "id_saida": row.id_saida,
        "timestamp": row.timestamp,
        "data_hora_acao": (op_ctx.ultimo_evento_ts if op_ctx else None) or row.timestamp,
        "acao": (op_ctx.acao_label if op_ctx else None) or "Sem ação",
        "executado_por": (op_ctx.executado_por if op_ctx else None) or "—",
        "sub_base": row.sub_base,
        "username": (op_ctx.ultimo_ator_username if op_ctx else None) or row.username,
        "entregador": nome_executor or row.entregador,
        "entregador_id": getattr(row, "entregador_id", None),
        "motoboy_id": getattr(row, "motoboy_id", None),
        "codigo": row.codigo,
        "servico": row.servico,
        "status": row.status,
        "base": row.base,
        "is_grande": getattr(row, "is_grande", False) or False,
    }


def _status_aliases_from_tokens(status_: Optional[List[str]]) -> List[str]:
    status_tokens_raw = [
        t for t in _parse_multi_values(status_) if _norm_text(t) not in {"", "(todos)", "todos", "all"}
    ]
    return sorted(
        {
            alias
            for token in status_tokens_raw
            for alias in _status_group_aliases(token)
        }
    )


def _servico_tokens_from_param(servico: Optional[List[str]]) -> List[str]:
    return [
        _norm_text(t)
        for t in _parse_multi_values(servico)
        if _norm_text(t) not in {"", "(todos)", "todos", "all"}
    ]


def _saida_passa_filtro_status(row: Saida, status_aliases: List[str]) -> bool:
    if not status_aliases:
        return True
    status_norm = _norm_text(row.status or "")
    return any(_norm_text(alias) == status_norm for alias in status_aliases)


def _saida_passa_filtro_servico(row: Saida, servico_tokens: List[str]) -> bool:
    if not servico_tokens:
        return True
    srv = (row.servico or "").strip().lower()
    for srv_norm in servico_tokens:
        if srv_norm == "shopee":
            if ("shopee" in srv) or ("spx" in srv):
                return True
        elif srv_norm in ("mercado livre", "mercadolivre", "mercado_livre", "mercado", "ml", "flex"):
            if (
                ("mercado livre" in srv)
                or ("mercado_livre" in srv)
                or ("mercadolivre" in srv)
                or (" ml" in f" {srv}")
                or ("flex" in srv)
            ):
                return True
        elif srv_norm == "avulso":
            if ("shopee" not in srv and "spx" not in srv) and not (
                ("mercado livre" in srv)
                or ("mercado_livre" in srv)
                or ("mercadolivre" in srv)
                or (" ml" in f" {srv}")
                or ("flex" in srv)
            ):
                return True
        elif _norm_text(row.servico or "") == srv_norm:
            return True
    return False


def _saida_passa_filtro_base(row: Saida, base: Optional[str]) -> bool:
    if not base or not base.strip() or base.lower() == "(todas)":
        return True
    return _norm_text(row.base or "") == _norm_text(base.strip())


def _saida_passa_filtro_periodo(
    row: Saida,
    ctx: Optional[SaidaOperacionalContext],
    de: Optional[date],
    ate: Optional[date],
) -> bool:
    if deve_excluir_saida_operacional(ctx):
        return False
    ts = timestamp_operacional_saida(ctx, row.timestamp)
    if ts is None:
        return False
    dia = ts.date()
    if de is not None and dia < de:
        return False
    if ate is not None and dia > ate:
        return False
    return True


def _build_acao_filter_sets(acao: Optional[List[str]]) -> Tuple[set, set]:
    acao_tokens = [
        _norm_text(t).replace("_", " ") for t in _parse_multi_values(acao)
        if _norm_text(t) not in {"", "(todos)", "todos", "all"}
    ]
    allowed_eventos: set = set()
    allowed_labels: set = set()
    for token in acao_tokens:
        token_norm = " ".join(token.split())
        token_key = token_norm.replace(" ", "_")
        allowed_eventos.add(token_key)
        allowed_labels.add(token_norm)
        label_canonico = rotulo_acao_evento(token_key)
        if label_canonico:
            allowed_labels.add(_norm_text(label_canonico))
        if token_key in {"reatribuido", "reatribuido_em_rota"}:
            allowed_eventos.add("reatribuido")
            allowed_eventos.add("reatribuido_em_rota")
            allowed_labels.add(_norm_text(rotulo_acao_evento("reatribuido") or ""))
            allowed_labels.add(_norm_text(rotulo_acao_evento("reatribuido_em_rota") or ""))
    return allowed_eventos, allowed_labels


def _saida_passa_filtro_acao(
    ctx: Optional[SaidaOperacionalContext],
    allowed_eventos: set,
    allowed_labels: set,
) -> bool:
    if not allowed_eventos and not allowed_labels:
        return True
    if ctx is None:
        return False
    equiv = _norm_text(_acao_equivalente(ctx.ultimo_evento)).replace("_", " ")
    if equiv in allowed_labels:
        return True
    if _norm_text(ctx.acao_label or "") in allowed_labels:
        return True
    if _acao_equivalente(ctx.ultimo_evento) in allowed_eventos:
        return True
    return False


def _listar_saidas_codigo_exato(
    db: Session,
    sub_base: str,
    codigo: str,
    de: Optional[date],
    ate: Optional[date],
    base: Optional[str],
    entregador: Optional[str],
    status_: Optional[List[str]],
    servico: Optional[List[str]],
    acao: Optional[List[str]],
    somente_g: Optional[bool],
    limit: Optional[int],
    offset: int,
) -> Dict[str, Any]:
    codigo_norm = _normalizar_codigo_busca_listar(codigo)
    row = _buscar_saida_codigo_exato(db, sub_base, codigo_norm)
    if row is None:
        return _listar_resposta_vazia()

    op_ctx_map = carregar_contexto_operacional(db, [row.id_saida])
    ctx = op_ctx_map.get(int(row.id_saida))

    entregador_filter_norm = ""
    if entregador and entregador.strip() and entregador.lower() != "(todos)":
        entregador_filter_norm = _norm_text(entregador)

    status_aliases = _status_aliases_from_tokens(status_)
    servico_tokens = _servico_tokens_from_param(servico)
    allowed_eventos, allowed_labels = _build_acao_filter_sets(acao)

    if somente_g and not (getattr(row, "is_grande", False) or False):
        return _listar_resposta_vazia()
    if not _saida_passa_filtro_base(row, base):
        return _listar_resposta_vazia()
    if not _saida_passa_filtro_status(row, status_aliases):
        return _listar_resposta_vazia()
    if not _saida_passa_filtro_servico(row, servico_tokens):
        return _listar_resposta_vazia()
    if not _saida_passa_filtro_periodo(row, ctx, de, ate):
        return _listar_resposta_vazia()
    if entregador_filter_norm:
        nome_exec = _nome_executor_atual(db, row) or row.entregador
        if _norm_text(nome_exec or "") != entregador_filter_norm:
            return _listar_resposta_vazia()
    if not _saida_passa_filtro_acao(ctx, allowed_eventos, allowed_labels):
        return _listar_resposta_vazia()

    sumShopee, sumMercado, sumAvulso = _contar_servico_listar(row.servico)
    items: List[Dict[str, Any]] = []
    if offset == 0 and (limit is None or limit > 0):
        nome_executor = _nome_executor_atual(db, row)
        items = [_montar_item_listar_saida(row, ctx, nome_executor)]

    return {
        "total": 1,
        "sumShopee": sumShopee,
        "sumMercado": sumMercado,
        "sumAvulso": sumAvulso,
        "items": items,
    }


# ============================================================
# GET — LISTAR SAÍDAS (COM CONTADORES)
# ============================================================

@router.get("/listar")
def listar_saidas(
    de: Optional[date] = Query(None),
    ate: Optional[date] = Query(None),
    base: Optional[str] = Query(None),
    username: Optional[str] = Query(None),
    entregador: Optional[str] = Query(None),
    status_: Optional[List[str]] = Query(None, alias="status"),
    codigo: Optional[str] = Query(None),
    servico: Optional[List[str]] = Query(None),
    acao: Optional[List[str]] = Query(None),
    localizar: Optional[str] = Query(None),
    somente_g: Optional[bool] = Query(None),
    codigo_exato: bool = Query(False),
    limit: Optional[int] = Query(None),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")

    if codigo and codigo.strip() and codigo_exato:
        return _listar_saidas_codigo_exato(
            db=db,
            sub_base=sub_base,
            codigo=codigo,
            de=de,
            ate=ate,
            base=base,
            entregador=entregador,
            status_=status_,
            servico=servico,
            acao=acao,
            somente_g=somente_g,
            limit=limit,
            offset=offset,
        )

    stmt = select(Saida).where(Saida.sub_base == sub_base)
    # Pré-filtro por janela de timestamp para reduzir cardinalidade antes do
    # processamento operacional em memória.
    dt_inicio = datetime.combine(de, datetime.min.time()) if de is not None else None
    dt_fim_exclusivo = datetime.combine(ate + timedelta(days=1), datetime.min.time()) if ate is not None else None
    eventos_operacionais = tuple(EVENTOS_ATRIBUICAO_VALIDOS)
    if de is not None:
        if dt_fim_exclusivo is not None:
            subq_hist_periodo = select(1).where(
                SaidaHistorico.id_saida == Saida.id_saida,
                SaidaHistorico.evento.in_(eventos_operacionais),
                SaidaHistorico.timestamp >= dt_inicio,
                SaidaHistorico.timestamp < dt_fim_exclusivo,
            )
            stmt = stmt.where(
                (
                    (Saida.timestamp >= dt_inicio)
                    & (Saida.timestamp < dt_fim_exclusivo)
                )
                | exists(subq_hist_periodo)
            )
        else:
            stmt = stmt.where(
                (Saida.timestamp >= dt_inicio)
                | exists(
                    select(1).where(
                        SaidaHistorico.id_saida == Saida.id_saida,
                        SaidaHistorico.evento.in_(eventos_operacionais),
                        SaidaHistorico.timestamp >= dt_inicio,
                    )
                )
            )
    elif ate is not None:
        subq_hist_ate = select(1).where(
            SaidaHistorico.id_saida == Saida.id_saida,
            SaidaHistorico.evento.in_(eventos_operacionais),
            SaidaHistorico.timestamp < dt_fim_exclusivo,
        )
        stmt = stmt.where(
            (Saida.timestamp < dt_fim_exclusivo)
            | exists(subq_hist_ate)
        )

    if base and base.strip() and base.lower() != "(todas)":
        base_norm = base.strip().lower()
        stmt = stmt.where(func.unaccent(func.lower(Saida.base)) == func.unaccent(base_norm))


    entregador_filter_norm = ""
    if entregador and entregador.strip() and entregador.lower() != "(todos)":
        entregador_filter_norm = _norm_text(entregador)

    status_tokens_raw = [
        t for t in _parse_multi_values(status_) if _norm_text(t) not in {"", "(todos)", "todos", "all"}
    ]
    status_aliases = sorted(
        {
            alias
            for token in status_tokens_raw
            for alias in _status_group_aliases(token)
        }
    )
    if status_aliases:
        conds_status = [
            func.unaccent(func.lower(Saida.status)) == func.unaccent(alias)
            for alias in status_aliases
        ]
        stmt = stmt.where(or_(*conds_status))

    servico_tokens = [
        _norm_text(t) for t in _parse_multi_values(servico) if _norm_text(t) not in {"", "(todos)", "todos", "all"}
    ]
    if servico_tokens:
        conds_srv = []
        for srv_norm in servico_tokens:
            if srv_norm == "shopee":
                conds_srv.append(_servico_is_shopee_expr(Saida.servico))
            elif srv_norm in ("mercado livre", "mercadolivre", "mercado_livre", "mercado", "ml", "flex"):
                conds_srv.append(_servico_is_mercado_expr(Saida.servico))
            elif srv_norm == "avulso":
                conds_srv.append(
                    (~_servico_is_shopee_expr(Saida.servico))
                    & (~_servico_is_mercado_expr(Saida.servico))
                )
            else:
                conds_srv.append(func.unaccent(func.lower(Saida.servico)) == func.unaccent(srv_norm))
        if conds_srv:
            stmt = stmt.where(or_(*conds_srv))

    if somente_g:
        stmt = stmt.where(Saida.is_grande.is_(True))

    if codigo and codigo.strip():
        codigo_trim = codigo.strip().upper()
        if codigo_exato:
            # Caminho otimizado para busca por código exato (aproveita índice em saidas.codigo).
            stmt = stmt.where(Saida.codigo == codigo_trim)
        else:
            # Fallback sem wildcard inicial para reduzir custo quando houver busca parcial.
            stmt = stmt.where(
                or_(
                    Saida.codigo == codigo_trim,
                    Saida.codigo.ilike(f"{codigo_trim}%"),
                )
            )
    elif localizar and localizar.strip():
        q = f"%{localizar.strip()}%"
        or_conds = or_(
            Saida.base.ilike(q),
            Saida.username.ilike(q),
            Saida.entregador.ilike(q),
            Saida.codigo.ilike(q),
            Saida.servico.ilike(q),
            Saida.status.ilike(q),
        )
        stmt = stmt.where(or_conds)

    rows_all = db.execute(stmt).scalars().all()
    rows_filtradas, op_ctx_map = filtrar_saidas_por_periodo_operacional(db, rows_all, de, ate)
    executor_nome_cache: Dict[int, Optional[str]] = {}

    # Evita N+1 em bases grandes: resolve nomes de motoboy em lote.
    motoboy_ids = sorted(
        {
            int(getattr(r, "motoboy_id"))
            for r in rows_filtradas
            if getattr(r, "motoboy_id", None) is not None
        }
    )
    motoboy_nome_map: Dict[int, str] = {}
    if motoboy_ids:
        rows_motoboy = []
        for motoboy_ids_lote in _chunked_ids(motoboy_ids):
            rows_lote = db.execute(
                select(Motoboy.id_motoboy, Motoboy.user_id).where(Motoboy.id_motoboy.in_(motoboy_ids_lote))
            ).all()
            rows_motoboy.extend(rows_lote)
        motoboy_user_map = {
            int(mid): (int(uid) if uid is not None else None)
            for mid, uid in rows_motoboy
        }
        user_ids = sorted({uid for uid in motoboy_user_map.values() if uid is not None})
        user_map: Dict[int, tuple] = {}
        if user_ids:
            rows_user = []
            for user_ids_lote in _chunked_ids(user_ids):
                rows_lote = db.execute(
                    select(User.id, User.nome, User.sobrenome, User.username).where(User.id.in_(user_ids_lote))
                ).all()
                rows_user.extend(rows_lote)
            user_map = {
                int(uid): ((nome or ""), (sobrenome or ""), (username or ""))
                for uid, nome, sobrenome, username in rows_user
            }

        for mid, uid in motoboy_user_map.items():
            if uid is None:
                motoboy_nome_map[mid] = f"Motoboy {mid}"
                continue
            nome, sobrenome, username_val = user_map.get(uid, ("", "", ""))
            nome_fmt = f"{nome} {sobrenome}".strip() or username_val or f"Motoboy {mid}"
            motoboy_nome_map[mid] = nome_fmt

    def _nome_executor_cached(saida: Saida) -> Optional[str]:
        sid = int(saida.id_saida)
        if sid not in executor_nome_cache:
            mid = getattr(saida, "motoboy_id", None)
            nome_motoboy = None
            if mid is not None:
                nome_motoboy = motoboy_nome_map.get(int(mid))
            executor_nome_cache[sid] = nome_motoboy or getattr(saida, "entregador", None)
        return executor_nome_cache[sid]

    if entregador_filter_norm:
        rows_filtradas = [
            r for r in rows_filtradas
            if _norm_text(_nome_executor_cached(r)) == entregador_filter_norm
        ]
    acao_tokens = [
        _norm_text(t).replace("_", " ") for t in _parse_multi_values(acao)
        if _norm_text(t) not in {"", "(todos)", "todos", "all"}
    ]
    if acao_tokens:
        allowed_eventos = set()
        allowed_labels = set()
        for token in acao_tokens:
            token_norm = " ".join(token.split())
            token_key = token_norm.replace(" ", "_")
            allowed_eventos.add(token_key)
            allowed_labels.add(token_norm)
            label_canonico = rotulo_acao_evento(token_key)
            if label_canonico:
                allowed_labels.add(_norm_text(label_canonico))
            if token_key in {"reatribuido", "reatribuido_em_rota"}:
                allowed_eventos.add("reatribuido")
                allowed_eventos.add("reatribuido_em_rota")
                allowed_labels.add(_norm_text(rotulo_acao_evento("reatribuido") or ""))
                allowed_labels.add(_norm_text(rotulo_acao_evento("reatribuido_em_rota") or ""))
        rows_filtradas = [
            r for r in rows_filtradas
            if (
                (
                    (ctx := op_ctx_map.get(r.id_saida)) is not None
                    and _norm_text(_acao_equivalente(ctx.ultimo_evento)).replace("_", " ") in allowed_labels
                )
                or (
                    (ctx := op_ctx_map.get(r.id_saida)) is not None
                    and _norm_text(ctx.acao_label) in allowed_labels
                )
                or (
                    (ctx := op_ctx_map.get(r.id_saida)) is not None
                    and _acao_equivalente(ctx.ultimo_evento) in allowed_eventos
                )
            )
        ]
    rows_filtradas.sort(
        key=lambda r: (
            ((op_ctx_map.get(r.id_saida).operacional_ts if op_ctx_map.get(r.id_saida) and op_ctx_map.get(r.id_saida).operacional_ts else None) or r.timestamp)
        ),
        reverse=True,
    )
    total = len(rows_filtradas)
    sumShopee = 0
    sumMercado = 0
    sumAvulso = 0
    for r in rows_filtradas:
        srv = (r.servico or "").strip().lower()
        if ("shopee" in srv) or ("spx" in srv):
            sumShopee += 1
        elif (
            ("mercado livre" in srv)
            or ("mercado_livre" in srv)
            or ("mercadolivre" in srv)
            or (" ml" in f" {srv}")
            or ("flex" in srv)
        ):
            sumMercado += 1
        else:
            sumAvulso += 1
    if limit is not None:
        start_idx = max(0, int(offset))
        end_idx = start_idx + max(0, int(limit))
        rows = rows_filtradas[start_idx:end_idx]
    else:
        rows = rows_filtradas[max(0, int(offset)):]

    nomes_executor = {int(r.id_saida): _nome_executor_cached(r) for r in rows}
    return {
        "total": total,
        "sumShopee": sumShopee,
        "sumMercado": sumMercado,
        "sumAvulso": sumAvulso,
        "items": [
            _montar_item_listar_saida(
                r,
                op_ctx_map.get(r.id_saida),
                nomes_executor.get(int(r.id_saida)) or r.entregador,
            )
            for r in rows
        ],
    }


# ============================================================
# GET — DETALHE COMPLETO (SAÍDA + SAIDAS_DETAIL)
# ============================================================

@router.get("/{id_saida}", response_model=SaidaDetalheCompletoOut)
def get_saida_detalhe(
    id_saida: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retorna a saída com o detalhe (saidas_detail) para a tela de registros."""
    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")
    obj = _get_owned_saida(db, sub_base, id_saida)
    detail_row = db.scalar(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida == id_saida)
        .order_by(SaidaDetail.id_detail.desc())
        .limit(1)
    )
    detail_out = None
    if detail_row:
        foto_urls_list: List[str] = []
        if detail_row.foto_url:
            raw = (detail_row.foto_url or "").strip()
            if raw.startswith("["):
                try:
                    foto_urls_list = json.loads(raw)
                    if not isinstance(foto_urls_list, list):
                        foto_urls_list = [raw]
                except (json.JSONDecodeError, TypeError):
                    foto_urls_list = [raw]
            else:
                foto_urls_list = [raw]
        foto_urls_list = [k for k in foto_urls_list if k][:3]
        detail_out = SaidaDetailOut(
            id_saida=detail_row.id_saida,
            id_entregador=detail_row.id_entregador,
            status=detail_row.status,
            tentativa=detail_row.tentativa,
            motivo_ocorrencia=detail_row.motivo_ocorrencia,
            observacao_ocorrencia=detail_row.observacao_ocorrencia,
            tipo_recebedor=detail_row.tipo_recebedor,
            nome_recebedor=detail_row.nome_recebedor,
            tipo_documento=detail_row.tipo_documento,
            numero_documento=detail_row.numero_documento,
            observacao_entrega=detail_row.observacao_entrega,
            dest_nome=detail_row.dest_nome,
            dest_rua=detail_row.dest_rua,
            dest_numero=detail_row.dest_numero,
            dest_complemento=detail_row.dest_complemento,
            dest_bairro=detail_row.dest_bairro,
            dest_cidade=detail_row.dest_cidade,
            dest_estado=detail_row.dest_estado,
            dest_cep=detail_row.dest_cep,
            dest_contato=detail_row.dest_contato,
            endereco_formatado=detail_row.endereco_formatado,
            endereco_origem=detail_row.endereco_origem,
            foto_urls=foto_urls_list or None,
        )
    executor_nome = _nome_executor_atual(db, obj) or obj.entregador
    return SaidaDetalheCompletoOut(
        id_saida=obj.id_saida,
        timestamp=obj.timestamp,
        data=obj.data,
        sub_base=obj.sub_base,
        username=obj.username,
        entregador=executor_nome,
        motoboy_id=obj.motoboy_id,
        data_hora_entrega=obj.data_hora_entrega,
        codigo=obj.codigo,
        servico=obj.servico,
        status=obj.status,
        base=obj.base,
        is_grande=getattr(obj, "is_grande", False) or False,
        detail=detail_out,
    )


# ============================================================
# PATCH — ADICIONAR FOTO (append até 3)
# ============================================================

class SaidaFotoPatchBody(BaseModel):
    foto_url: str = Field(min_length=1)
    status: str = Field(pattern="^(entregue|ausente)$")
    validar_campos_obrigatorios: bool = True
    alterar_status: bool = True


def _normalize_foto_url_to_key(foto_url: str) -> str:
    """Se for URL completa, extrai object_key; senão retorna como está."""
    s = (foto_url or "").strip()
    if not s:
        return s
    if s.startswith("http://") or s.startswith("https://"):
        bucket = os.getenv("B2_BUCKET_NAME", "ts-prod-entregas-fotos")
        prefix = f"/{bucket}/"
        idx = s.find(prefix)
        if idx != -1:
            return s[idx + len(prefix) :].split("?")[0]
        return s.split("/")[-1].split("?")[0] or s
    return s


@router.patch("/{id_saida}/foto")
def patch_saida_foto(
    id_saida: int,
    body: SaidaFotoPatchBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Append uma foto (object_key) à lista em saidas_detail; atualiza status da saída. Máx. 3 fotos."""
    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")
    obj = _get_owned_saida(db, sub_base, id_saida)
    status_norm = normalizar_status_saida(obj.status)
    if _status_esta_finalizado(status_norm) and body.alterar_status:
        raise HTTPException(status_code=422, detail=_status_finalizado_detail(obj, status_norm))
    detail_row = db.scalar(
        select(SaidaDetail).where(SaidaDetail.id_saida == id_saida).order_by(SaidaDetail.id_detail.desc()).limit(1)
    )
    key = _normalize_foto_url_to_key(body.foto_url)
    if not key:
        raise HTTPException(status_code=422, detail="foto_url inválida.")

    status_canon = STATUS_ENTREGUE if body.status.lower() == "entregue" else STATUS_AUSENTE
    current_list: List[str] = []
    if detail_row and detail_row.foto_url:
        raw = (detail_row.foto_url or "").strip()
        if raw.startswith("["):
            try:
                current_list = json.loads(raw)
                if not isinstance(current_list, list):
                    current_list = [raw]
            except (json.JSONDecodeError, TypeError):
                current_list = [raw]
        else:
            current_list = [raw]
    current_list = [k for k in current_list if k]

    if len(current_list) >= 3:
        raise HTTPException(status_code=422, detail="Máximo de 3 fotos por entrega.")

    current_list.append(key)
    payload = json.dumps(current_list)

    if detail_row:
        detail_row.foto_url = payload
        detail_row.status = status_canon
    else:
        detail_row = SaidaDetail(
            id_saida=id_saida,
            id_entregador=getattr(obj, "motoboy_id", None) or 0,
            status=status_canon,
            tentativa=1,
            foto_url=payload,
        )
        db.add(detail_row)

    if body.validar_campos_obrigatorios and body.alterar_status:
        contexto_validacao = "ENTREGUE" if status_canon == STATUS_ENTREGUE else "AUSENTE"
        faltantes = validate_campos_obrigatorios_conclusao(
            db,
            saida=obj,
            contexto=contexto_validacao,
            detail=detail_row,
            overrides={"foto_url": payload},
        )
        raise_if_campos_obrigatorios_faltando(faltantes)

    if body.alterar_status:
        status_anterior = obj.status
        obj.status = status_canon
        db.add(
            SaidaHistorico(
                id_saida=id_saida,
                evento=body.status.lower(),
                status_anterior=status_anterior,
                status_novo=status_canon,
                user_id=current_user.id,
            )
        )
    db.commit()
    logger.info(
        "PATCH foto: id_saida=%s object_key=%s status=%s foto_urls_count=%s user_id=%s",
        id_saida,
        key,
        body.status,
        len(current_list),
        getattr(current_user, "id", None),
    )
    return {"ok": True, "foto_urls": current_list}


# ============================================================
# GET — HISTÓRICO DA SAÍDA
# ============================================================

@router.get("/{id_saida}/historico", response_model=list[SaidaHistoricoItemOut])
def get_saida_historico(
    id_saida: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista eventos do histórico da saída (saida_historico), ordenados por timestamp. Inclui usuario_nome quando há user_id."""
    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")
    _get_owned_saida(db, sub_base, id_saida)
    return listar_historico_saida(db, id_saida)


# ============================================================
# PATCH — ATUALIZAR SAÍDA
# ============================================================

@router.patch("/{id_saida}", response_model=SaidaOut)
def atualizar_saida(
    id_saida: int,
    payload: SaidaUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = current_user.sub_base
    obj = _get_owned_saida(db, sub_base, id_saida)
    status_anterior = normalizar_status_saida(obj.status)
    _validar_alteracao_saida_finalizada(obj, status_anterior, payload)
    motoboy_anterior = obj.motoboy_id
    entregador_anterior = (obj.entregador or "").strip()
    payload_changed = {
        "status": False,
        "motoboy": False,
        "executor": False,
    }

    if payload.codigo is not None:
        novo = payload.codigo.strip()
        dup = db.scalar(
            select(Saida.id_saida).where(
                Saida.sub_base == sub_base,
                Saida.codigo == novo,
                Saida.id_saida != obj.id_saida
            )
        )
        if dup:
            raise HTTPException(409, f"Código '{novo}' já registrado.")
        obj.codigo = novo

    if payload.entregador_id is not None or (payload.entregador is not None and payload.entregador.strip()):
        try:
            entregador_id, entregador_nome = _resolve_entregador(
                db, sub_base,
                entregador_id=payload.entregador_id,
                entregador_nome=payload.entregador,
            )
            obj.entregador_id = entregador_id
            obj.entregador = entregador_nome
            payload_changed["executor"] = (
                payload_changed["executor"]
                or (entregador_nome or "").strip() != entregador_anterior
            )
        except HTTPException as e:
            # Se não encontrou entregador pelo nome mas a saída já tem entregador/motoboy,
            # manter o atual e aplicar só status/serviço/etc (evita falha ao editar só status ex.: cancelado)
            detail = e.detail if isinstance(e.detail, dict) else {}
            if e.status_code == 422 and detail.get("code") == "ENTREGADOR_NAO_ENCONTRADO":
                if (getattr(obj, "entregador_id", None) is not None) or (getattr(obj, "motoboy_id", None) is not None):
                    pass  # mantém entregador/motoboy atual
                else:
                    raise
            else:
                raise

    reatribuicao_entregue = (
        status_anterior == STATUS_ENTREGUE
        and payload.motoboy_id is not None
        and int(payload.motoboy_id) != int(motoboy_anterior or 0)
    )
    if reatribuicao_entregue:
        motoboy = _resolve_motoboy_for_subbase(db, sub_base, payload.motoboy_id)
        obj.motoboy_id = motoboy.id_motoboy
        obj.entregador = payload.entregador or _get_motoboy_nome(db, motoboy)
        obj.entregador_id = None
        obj.status = STATUS_EM_ROTA
        obj.data_hora_entrega = None
        _aplicar_detail_reatribuicao_entregue(db, obj.id_saida, motoboy.id_motoboy, STATUS_EM_ROTA)
        payload_changed["motoboy"] = True
        payload_changed["executor"] = True
        payload_changed["status"] = True
    elif payload.motoboy_id is not None:
        motoboy = _resolve_motoboy_for_subbase(db, sub_base, payload.motoboy_id)
        obj.motoboy_id = motoboy.id_motoboy
        obj.entregador = payload.entregador or _get_motoboy_nome(db, motoboy)
        obj.status = STATUS_SAIU_PARA_ENTREGA
        payload_changed["motoboy"] = (motoboy_anterior != obj.motoboy_id)
        payload_changed["executor"] = payload_changed["executor"] or payload_changed["motoboy"]
        payload_changed["status"] = True

    if payload.status is not None:
        novo_status = normalizar_status_saida(payload.status)
        if novo_status in (STATUS_ENTREGUE, STATUS_AUSENTE):
            detail_row = db.scalar(
                select(SaidaDetail)
                .where(SaidaDetail.id_saida == id_saida)
                .order_by(SaidaDetail.id_detail.desc())
                .limit(1)
            )
            contexto_validacao = "ENTREGUE" if novo_status == STATUS_ENTREGUE else "AUSENTE"
            faltantes = validate_campos_obrigatorios_conclusao(
                db,
                saida=obj,
                contexto=contexto_validacao,
                detail=detail_row,
            )
            raise_if_campos_obrigatorios_faltando(faltantes)
        payload_changed["status"] = payload_changed["status"] or (novo_status != normalizar_status_saida(obj.status))
        obj.status = novo_status
        # Se alterou para cancelado, marcar cobrança como cancelada (não contabilizada)
        if novo_status == STATUS_CANCELADO:
            itens = db.scalars(
                select(OwnerCobrancaItem).where(OwnerCobrancaItem.id_saida == obj.id_saida)
            ).all()
            for item in itens:
                item.cancelado = True

    if payload.servico is not None:
        obj.servico = canonicalize_servico(payload.servico)

    if payload.base is not None:
        obj.base = payload.base.strip()

    if payload.is_grande is not None:
        role = getattr(current_user, "role", None)
        if role not in (0, 1, 2):
            raise HTTPException(
                403,
                "Apenas admin, root ou operador podem marcar ou desmarcar pacote como G (Grande).",
            )
        obj.is_grande = bool(payload.is_grande)

    status_novo = normalizar_status_saida(obj.status)
    status_mudou = status_novo != status_anterior
    motoboy_novo = obj.motoboy_id
    executor_mudou = payload_changed["executor"] or ((obj.entregador or "").strip() != entregador_anterior)
    evento_historico = None
    if status_mudou:
        evento_historico = _evento_status_manual(status_novo)
    if executor_mudou and evento_historico not in {"cancelado", "entregue", "ausente"}:
        evento_historico = "reatribuido"

    if evento_historico:
        db.add(
            SaidaHistorico(
                id_saida=obj.id_saida,
                evento=evento_historico,
                status_anterior=status_anterior,
                status_novo=status_novo,
                motoboy_id_anterior=motoboy_anterior,
                motoboy_id_novo=motoboy_novo,
                user_id=current_user.id,
            )
        )

    try:
        db.commit()
        db.refresh(obj)
    except Exception:
        db.rollback()
        raise HTTPException(500, "Erro ao atualizar saída.")

    return SaidaOut.model_validate(obj)


# ============================================================
# DELETE — EXCLUIR SAÍDA
# ============================================================

@router.delete("/{id_saida}", status_code=204)
def deletar_saida(
    id_saida: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = current_user.sub_base
    obj = _get_owned_saida(db, sub_base, id_saida)

    _check_delete_window_or_409(obj.timestamp)

    try:
        db.delete(obj)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "Erro ao deletar saída.")

    return
