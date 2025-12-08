from __future__ import annotations

from typing import Optional, List
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, Owner, Saida
from sqlalchemy import func


router = APIRouter(prefix="/saidas", tags=["Saídas"])


# ============================================================
# NORMALIZADORES
# ============================================================

def norm_str(x: str | None) -> str | None:
    """Remove espaços, baixa caixa e normaliza unicode."""
    if not x:
        return None
    return x.strip().casefold()


# ============================================================
# SCHEMAS
# ============================================================

class SaidaCreate(BaseModel):
    entregador: str = Field(min_length=1)
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


class SaidaListOut(BaseModel):
    total: int
    items: List[SaidaGridItem]
    model_config = ConfigDict(from_attributes=True)


class SaidaUpdate(BaseModel):
    entregador: Optional[str] = Field(None)
    status: Optional[str] = Field(None)
    codigo: Optional[str] = Field(None)
    servico: Optional[str] = Field(None)
    base: Optional[str] = Field(None)


# ============================================================
# HELPERS
# ============================================================

def _resolve_user_base(db: Session, current_user: User) -> str:
    """Resolve a sub_base do usuário logado."""
    for field in ("id", "email", "username"):
        value = getattr(current_user, field, None)
        if value:
            u = db.scalars(select(User).where(getattr(User, field) == value)).first()
            if u and u.sub_base:
                return u.sub_base

    raise HTTPException(401, "Usuário sem sub_base definida.")


def _get_owned_saida(db: Session, sub_base_user: str, id_saida: int) -> Saida:
    obj = db.get(Saida, id_saida)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(
            404,
            {"code": "SAIDA_NOT_FOUND", "message": "Saída não encontrada."}
        )
    return obj


def _check_delete_window_or_409(ts: datetime):
    if ts is None:
        raise HTTPException(409, {"code": "DELETE_WINDOW_EXPIRED", "message": "Janela expirada."})

    if datetime.utcnow() - ts > timedelta(days=1):
        raise HTTPException(409, {"code": "DELETE_WINDOW_EXPIRED", "message": "Janela expirada."})


# ============================================================
# POST /registrar
# ============================================================

@router.post("/registrar", status_code=201)
def registrar_saida(
    payload: SaidaCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    username = getattr(current_user, "username", None)
    if not username:
        raise HTTPException(401, "Usuário sem username.")

    sub_base_user = _resolve_user_base(db, current_user)

    codigo = payload.codigo.strip()
    entregador = payload.entregador.strip()
    servico_norm = payload.servico.strip().title()
    status_norm = payload.status.strip() if payload.status else "Saiu para entrega"

    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base_user))
    if not owner:
        raise HTTPException(404, "Owner não encontrado.")

    ignorar = bool(owner.ignorar_coleta)

    existente = db.scalars(
        select(Saida).where(Saida.sub_base == sub_base_user, Saida.codigo == codigo)
    ).first()

    if existente:
        raise HTTPException(
            409,
            {"code": "DUPLICATE_SAIDA", "message": f"Código '{codigo}' já registrado."}
        )

    if not ignorar:
        from models import Coleta
        coleta_exists = db.scalar(
            select(Coleta).where(
                Coleta.sub_base == sub_base_user,
                Coleta.username_entregador == entregador
            )
        )
        if not coleta_exists:
            raise HTTPException(
                409,
                {"code": "COLETA_OBRIGATORIA", "message": "Cliente exige coleta antes."}
            )

    try:
        row = Saida(
            sub_base=sub_base_user,
            username=username,
            entregador=entregador,
            codigo=codigo,
            servico=servico_norm,
            status=status_norm,
        )

        db.add(row)
        db.commit()
        db.refresh(row)

        if ignorar:
            try:
                from models import OwnerCobrancaItem

                item = OwnerCobrancaItem(
                    sub_base=sub_base_user,
                    id_coleta=None,
                    id_saida=row.id_saida,
                    valor=owner.valor
                )
                db.add(item)
                db.commit()

            except Exception as e:
                db.rollback()
                print("[COBRANÇA] erro:", e)

    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Erro ao registrar saída: {e}")

    return SaidaOut.model_validate(row)


# ============================================================
# GET /listar
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
    sub_base_user = _resolve_user_base(db, current_user)

    filtered_stmt = select(Saida).where(Saida.sub_base == sub_base_user)

    # normalizadores SQL
    srv = func.lower(func.trim(Saida.servico))
    sts = func.lower(func.trim(Saida.status))
    bs = func.lower(func.trim(Saida.base))

    # FILTRO BASE
    if base and base.strip() and base.lower() != "(todas)":
        filtered_stmt = filtered_stmt.where(bs == base.strip().lower())

    # FILTRO DATA
    if de:
        filtered_stmt = filtered_stmt.where(
            Saida.timestamp >= datetime.combine(de, datetime.min.time())
        )
    if ate:
        filtered_stmt = filtered_stmt.where(
            Saida.timestamp <= datetime.combine(ate, datetime.max.time())
        )

    # FILTRO ENTREGADOR
    if entregador and entregador.strip() and entregador.lower() != "(todos)":
        filtered_stmt = filtered_stmt.where(
            func.lower(func.trim(Saida.entregador)) == entregador.strip().lower()
        )

    # FILTRO STATUS
    if status_ and status_.strip() and status_.lower() != "(todos)":
       status_norm = status_.strip().lower()
         filtered_stmt = filtered_stmt.where(
         func.unaccent(func.lower(Saida.status)) == func.unaccent(status_norm)
        )

    # FILTRO CÓDIGO
    if codigo and codigo.strip():
        filtered_stmt = filtered_stmt.where(
            func.lower(Saida.codigo).ilike(f"%{codigo.strip().lower()}%")
        )

    # TOTAL
    total = db.scalar(select(func.count()).select_from(filtered_stmt.subquery())) or 0

    subq = filtered_stmt.subquery()
    srv_q = func.lower(func.trim(subq.c.servico))

    sumShopee = db.scalar(
        select(func.count()).select_from(subq).where(srv_q == "shopee")
    ) or 0

    sumMercado = db.scalar(
        select(func.count()).select_from(subq).where(srv_q == "mercado livre")
    ) or 0

    sumAvulso = db.scalar(
        select(func.count()).select_from(subq).where(
            (srv_q != "shopee") & (srv_q != "mercado livre")
        )
    ) or 0

    # paginação
    stmt = filtered_stmt.order_by(Saida.timestamp.desc())

    if limit:
        stmt = stmt.limit(limit)
    if offset:
        stmt = stmt.offset(offset)

    rows = db.execute(stmt).scalars().all()

    items = [
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
    ]

    return {
        "total": total,
        "sumShopee": sumShopee,
        "sumMercado": sumMercado,
        "sumAvulso": sumAvulso,
        "items": items,
    }


# ============================================================
# PATCH /id
# ============================================================

@router.patch("/{id_saida}", response_model=SaidaOut)
def atualizar_saida(
    id_saida: int,
    payload: SaidaUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_saida(db, sub_base_user, id_saida)

    if not any([payload.codigo, payload.entregador, payload.status, payload.servico, payload.base]):
        raise HTTPException(422, {"code": "NO_FIELDS_TO_UPDATE", "message": "Nenhum campo enviado."})

    try:
        if payload.codigo is not None:
            novo = payload.codigo.strip()
            if not novo:
                raise HTTPException(422, "Código vazio.")
            # verificar duplicidade
            dup = db.scalars(
                select(Saida).where(
                    Saida.sub_base == obj.sub_base,
                    Saida.codigo == novo,
                    Saida.id_saida != obj.id_saida,
                )
            ).first()
            if dup:
                raise HTTPException(409, f"Código '{novo}' já existe.")
            obj.codigo = novo

        if payload.entregador is not None:
            obj.entregador = payload.entregador.strip()

        if payload.status is not None:
            obj.status = payload.status.strip()

        if payload.servico is not None:
            obj.servico = payload.servico.strip().title()

        if payload.base is not None:
            obj.base = payload.base.strip().title()

        db.add(obj)
        db.commit()
        db.refresh(obj)

    except HTTPException:
        db.rollback()
        raise

    except Exception:
        db.rollback()
        raise HTTPException(500, {"code": "UPDATE_FAILED", "message": "Erro ao atualizar."})

    return SaidaOut.model_validate(obj)


# ============================================================
# DELETE /id
# ============================================================

@router.delete("/{id_saida}", status_code=204)
def deletar_saida(
    id_saida: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_saida(db, sub_base_user, id_saida)

    _check_delete_window_or_409(obj.timestamp)

    try:
        db.delete(obj)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "Erro ao deletar saída.")

    return
