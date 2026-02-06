from __future__ import annotations

import unicodedata
from typing import Optional
from datetime import datetime, date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, Saida, Coleta, Entregador, OwnerCobrancaItem


# ============================================================
# ROTAS DE SAÍDAS
# ============================================================

router = APIRouter(prefix="/saidas", tags=["Saídas"])


# ============================================================
# SCHEMAS
# ============================================================

class SaidaCreate(BaseModel):
    entregador_id: Optional[int] = None
    entregador: Optional[str] = None
    codigo: str = Field(min_length=1)
    servico: str = Field(min_length=1)
    status: Optional[str] = None


class SaidaOut(BaseModel):
    id_saida: int
    timestamp: datetime
    data: date
    sub_base: Optional[str]
    username: Optional[str]
    entregador: Optional[str]
    codigo: Optional[str]
    servico: Optional[str]
    status: Optional[str]
    base: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class SaidaGridItem(BaseModel):
    id_saida: int
    timestamp: datetime
    username: Optional[str]
    entregador: Optional[str]
    codigo: Optional[str]
    servico: Optional[str]
    status: Optional[str]
    base: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class SaidaUpdate(BaseModel):
    entregador_id: Optional[int] = None
    entregador: Optional[str] = None
    status: Optional[str] = None
    codigo: Optional[str] = None
    servico: Optional[str] = None
    base: Optional[str] = None


class SaidaLerIn(BaseModel):
    """Payload para POST /saidas/ler — leitura unificada (1 SELECT + 1 INSERT ou UPDATE)."""
    codigo: str = Field(min_length=1)
    entregador_id: Optional[int] = None
    entregador: Optional[str] = None
    servico: str = Field(min_length=1)


# ============================================================
# HELPERS
# ============================================================

def _normalizar_nome(s: str) -> str:
    """Lower + unaccent para comparação de nome de entregador."""
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def normalizar_status_saida(raw: Optional[str]) -> str:
    """Status canônico em lowercase. Banco é fonte da verdade."""
    s = (raw or "").strip().lower()
    if not s:
        return "saiu"
    if s in ("saiu", "saiu para entrega", "saiu pra entrega"):
        return "saiu"
    if s in ("cancelado", "cancelada"):
        return "cancelado"
    if s in ("coletado", "coletada"):
        return "coletado"
    return s


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
    servico = payload.servico.strip().title()
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

    try:
        row = Saida(
            sub_base=sub_base,
            username=username,
            entregador=entregador_nome,
            entregador_id=entregador_id,
            codigo=codigo,
            servico=servico,
            status=status_val,
        )
        db.add(row)
        db.flush()

        if ignorar_coleta:
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

    entregador_id, entregador_nome = _resolve_entregador(
        db, sub_base,
        entregador_id=payload.entregador_id,
        entregador_nome=payload.entregador,
    )

    codigo = payload.codigo.strip()
    servico = payload.servico.strip().title()

    # 1 SELECT por (sub_base, codigo) — índice existente, O(1)
    existente = db.scalar(
        select(Saida).where(
            Saida.sub_base == sub_base,
            Saida.codigo == codigo,
        )
    )

    if existente is None:
        # Não existe: ignorar_coleta → INSERT; senão 422 (erro de negócio, sem retry)
        if not ignorar_coleta:
            # JSONResponse direto para preservar código de erro sem passar pelo handler global.
            return JSONResponse(
                status_code=422,
                content={"code": "NAO_COLETADO", "message": "Código não coletado."},
            )
        try:
            row = Saida(
                sub_base=sub_base,
                username=username,
                entregador=entregador_nome,
                entregador_id=entregador_id,
                codigo=codigo,
                servico=servico,
                status="saiu",
            )
            db.add(row)
            db.flush()
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

    if status_norm == "coletado":
        # coletado → UPDATE para saiu
        existente.status = "saiu"
        existente.entregador_id = entregador_id
        existente.entregador = entregador_nome
        try:
            db.commit()
            db.refresh(existente)
            return SaidaOut.model_validate(existente)
        except Exception:
            db.rollback()
            raise HTTPException(500, "Erro ao atualizar saída.")

    if status_norm == "saiu":
        # mesmo entregador → 200 idempotente (sem 409)
        mesmo_ent = (
            (entregador_id is not None and existente.entregador_id == entregador_id)
            or _normalizar_nome(entregador_nome or "") == _normalizar_nome(existente.entregador or "")
        )
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
    status_: Optional[str] = Query(None, alias="status"),
    codigo: Optional[str] = Query(None),
    limit: Optional[int] = Query(None),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = current_user.sub_base
    stmt = select(Saida).where(Saida.sub_base == sub_base)

    if base and base.strip() and base.lower() != "(todas)":
        base_norm = base.strip().lower()
        stmt = stmt.where(func.unaccent(func.lower(Saida.base)) == func.unaccent(base_norm))

    if de:
        stmt = stmt.where(Saida.timestamp >= datetime.combine(de, datetime.min.time()))
    if ate:
        stmt = stmt.where(Saida.timestamp <= datetime.combine(ate, datetime.max.time()))

    if entregador and entregador.strip() and entregador.lower() != "(todos)":
        ent_norm = entregador.strip().lower()
        stmt = stmt.where(func.unaccent(func.lower(Saida.entregador)) == func.unaccent(ent_norm))

    if status_ and status_.strip() and status_.lower() != "(todos)":
        st_norm = status_.strip().lower()
        stmt = stmt.where(func.unaccent(func.lower(Saida.status)) == func.unaccent(st_norm))

    if codigo and codigo.strip():
        stmt = stmt.where(Saida.codigo.ilike(f"%{codigo.strip()}%"))

    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)

    subq = stmt.subquery()

    sumShopee = db.scalar(
        select(func.count()).select_from(subq)
        .where(func.unaccent(func.lower(subq.c.servico)) == func.unaccent("shopee"))
    ) or 0

    sumMercado = db.scalar(
        select(func.count()).select_from(subq)
        .where(func.unaccent(func.lower(subq.c.servico)) == func.unaccent("mercado livre"))
    ) or 0

    sumAvulso = db.scalar(
        select(func.count()).select_from(subq)
        .where(
            (func.unaccent(func.lower(subq.c.servico)) != func.unaccent("shopee")) &
            (func.unaccent(func.lower(subq.c.servico)) != func.unaccent("mercado livre"))
        )
    ) or 0

    stmt = stmt.order_by(Saida.timestamp.desc())
    if limit:
        stmt = stmt.limit(limit)
    if offset:
        stmt = stmt.offset(offset)

    rows = db.execute(stmt).scalars().all()

    return {
        "total": total,
        "sumShopee": sumShopee,
        "sumMercado": sumMercado,
        "sumAvulso": sumAvulso,
        "items": [
            {
                "id_saida": r.id_saida,
                "timestamp": r.timestamp,
                "username": r.username,
                "entregador": r.entregador,
                "codigo": r.codigo,
                "servico": r.servico,
                "status": r.status,
                "base": r.base,
            }
            for r in rows
        ],
    }


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
        entregador_id, entregador_nome = _resolve_entregador(
            db, sub_base,
            entregador_id=payload.entregador_id,
            entregador_nome=payload.entregador,
        )
        obj.entregador_id = entregador_id
        obj.entregador = entregador_nome

    if payload.status is not None:
        obj.status = normalizar_status_saida(payload.status)

    if payload.servico is not None:
        obj.servico = payload.servico.strip().title()

    if payload.base is not None:
        obj.base = payload.base.strip()

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
