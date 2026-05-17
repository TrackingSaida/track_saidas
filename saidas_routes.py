from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from typing import Optional, List

logger = logging.getLogger(__name__)
from datetime import datetime, date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, func, or_
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
)
from codigo_normalizer import canonicalize_servico


# ============================================================
# ROTAS DE SAÍDAS
# ============================================================

router = APIRouter(prefix="/saidas", tags=["Saídas"])
MAX_IDS_POR_LOTE = 5000


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


class SaidaHistoricoItemOut(BaseModel):
    """Um item do histórico para GET /saidas/{id_saida}/historico."""
    id: int
    id_saida: int
    evento: str
    timestamp: datetime
    status_anterior: Optional[str] = None
    status_novo: Optional[str] = None
    user_id: Optional[int] = None
    usuario_nome: Optional[str] = None
    motoboy_id_anterior: Optional[int] = None
    motoboy_id_novo: Optional[int] = None
    acao_label: Optional[str] = None
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

    codigo = payload.codigo.strip()
    servico = canonicalize_servico(payload.servico)

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
        qr_raw = getattr(payload, "qr_payload_raw", None)
        store_qr = _should_store_qr_payload_raw(servico, qr_raw)
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
                qr_payload_raw=qr_raw.strip() if store_qr and qr_raw else None,
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
            return SaidaOut.model_validate(existente)
        # outro entregador → 409 para front acionar PATCH de troca.
        # Sem retry: front trata com Swal + PATCH, evita latência de retry em fluxo normal.
        return JSONResponse(
            status_code=409,
            content={
                "code": "TROCA_ENTREGADOR",
                "id_saida": existente.id_saida,
                "message": "Código já saiu com outro entregador.",
                "entregador_atual": existente.entregador,
                "username": existente.username,
            },
        )

    # status cancelado ou outro: retornar como está (idempotente) ou 422 conforme regra de negócio
    return SaidaOut.model_validate(existente)


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
    rows = rows_filtradas[offset : (offset + limit) if limit else None]

    nomes_executor = {int(r.id_saida): _nome_executor_cached(r) for r in rows}
    return {
        "total": total,
        "sumShopee": sumShopee,
        "sumMercado": sumMercado,
        "sumAvulso": sumAvulso,
        "items": [
            {
                "id_saida": r.id_saida,
                "timestamp": r.timestamp,
                "data_hora_acao": (op_ctx_map.get(r.id_saida).ultimo_evento_ts if op_ctx_map.get(r.id_saida) else None) or r.timestamp,
                "acao": (op_ctx_map.get(r.id_saida).acao_label if op_ctx_map.get(r.id_saida) else None) or "Sem ação",
                "sub_base": r.sub_base,
                "username": (op_ctx_map.get(r.id_saida).ultimo_ator_username if op_ctx_map.get(r.id_saida) else None) or r.username,
                "entregador": nomes_executor.get(int(r.id_saida)) or r.entregador,
                "entregador_id": getattr(r, "entregador_id", None),
                "motoboy_id": getattr(r, "motoboy_id", None),
                "codigo": r.codigo,
                "servico": r.servico,
                "status": r.status,
                "base": r.base,
                "is_grande": getattr(r, "is_grande", False) or False,
            }
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
        select(SaidaDetail).where(SaidaDetail.id_saida == id_saida).limit(1)
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
    rows = db.execute(
        select(SaidaHistorico, User.username)
        .outerjoin(User, SaidaHistorico.user_id == User.id)
        .where(SaidaHistorico.id_saida == id_saida)
        .order_by(SaidaHistorico.timestamp.asc())
    ).all()
    out = []
    for row in rows:
        h, username = row[0], row[1]
        evento_norm = (h.evento or "").strip().lower()
        out.append(SaidaHistoricoItemOut(
            id=h.id,
            id_saida=h.id_saida,
            evento=h.evento,
            timestamp=h.timestamp,
            status_anterior=h.status_anterior,
            status_novo=h.status_novo,
            user_id=h.user_id,
            usuario_nome=username,
            motoboy_id_anterior=h.motoboy_id_anterior,
            motoboy_id_novo=h.motoboy_id_novo,
            acao_label=rotulo_acao_evento(evento_norm),
        ))
    return out


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

    if payload.motoboy_id is not None:
        motoboy = _resolve_motoboy_for_subbase(db, sub_base, payload.motoboy_id)
        obj.motoboy_id = motoboy.id_motoboy
        obj.entregador = payload.entregador or _get_motoboy_nome(db, motoboy)
        obj.status = STATUS_SAIU_PARA_ENTREGA

    if payload.status is not None:
        novo_status = normalizar_status_saida(payload.status)
        obj.status = novo_status
        # Se alterou para cancelado, marcar cobrança como cancelada (não contabilizada)
        if novo_status == "cancelado":
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
