from __future__ import annotations

from typing import Optional, List
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, Owner, Saida


# ============================================================
# ROTAS DE SAÍDAS
# ============================================================

router = APIRouter(prefix="/saidas", tags=["Saídas"])


# ---------- SCHEMAS ----------
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


class SaidaUpdate(BaseModel):
    entregador: Optional[str] = Field(None)
    status: Optional[str] = Field(None)
    codigo: Optional[str] = Field(None)


# ---------- HELPERS ----------
def _resolve_user_base(db: Session, current_user: User) -> str:
    user_id = getattr(current_user, "id", None)
    if user_id:
        u = db.get(User, user_id)
        if u and u.sub_base:
            return u.sub_base

    email = getattr(current_user, "email", None)
    if email:
        u = db.scalars(select(User).where(User.email == email)).first()
        if u and u.sub_base:
            return u.sub_base

    username = getattr(current_user, "username", None)
    if username:
        u = db.scalars(select(User).where(User.username == username)).first()
        if u and u.sub_base:
            return u.sub_base

    raise HTTPException(status_code=401, detail="Usuário sem sub_base definida.")


def _get_owned_saida(db: Session, sub_base_user: str, id_saida: int) -> Saida:
    obj = db.get(Saida, id_saida)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(
            status_code=404,
            detail={"code": "SAIDA_NOT_FOUND", "message": "Saída não encontrada."}
        )
    return obj


def _check_delete_window_or_409(ts: datetime):
    if ts is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "DELETE_WINDOW_EXPIRED", "message": "Janela para exclusão expirada."}
        )

    agora = datetime.utcnow()
    if agora - ts > timedelta(days=1):
        raise HTTPException(
            status_code=409,
            detail={"code": "DELETE_WINDOW_EXPIRED", "message": "Janela para exclusão expirada."}
        )


# ---------- POST: REGISTRAR SAÍDA ----------
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

    # sub_base do usuário logado
    sub_base_user = _resolve_user_base(db, current_user)

    # normalizar payload
    codigo = payload.codigo.strip()
    entregador = payload.entregador.strip()
    servico = payload.servico.strip()
    status_val = payload.status.strip() if payload.status else "Saiu para entrega"

    # -----------------------------------------
    # Buscar OWNER (para ignorar_coleta E valor)
    # -----------------------------------------
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base_user))
    if not owner:
        raise HTTPException(404, "Owner não encontrado para esta sub_base.")

    ignorar = bool(owner.ignorar_coleta)
    # Debug opcional
    print(f"[DEBUG] ignorar_coleta={ignorar} sub_base={sub_base_user}")

    # -----------------------------------------
    # DUPLICIDADE
    # -----------------------------------------
    existente = db.scalars(
        select(Saida).where(
            Saida.sub_base == sub_base_user,
            Saida.codigo == codigo
        )
    ).first()
    if existente:
        raise HTTPException(
            409,
            {"code": "DUPLICATE_SAIDA", "message": f"Código '{codigo}' já registrado."}
        )

    # -----------------------------------------
    # SE NÃO IGNORAR → coleta obrigatória
    # mas permite registrar com status "Não Coletado"
    # -----------------------------------------
    if not ignorar:
        from models import Coleta
        coleta_exists = db.scalar(
            select(Coleta).where(
                Coleta.sub_base == sub_base_user,
                Coleta.username_entregador == entregador
            )
        )
        if not coleta_exists:
            print(f"[AVISO] Sem coleta encontrada para {entregador} — registrando como 'Não Coletado'.")
            status_val = "Não Coletado"

    # -----------------------------------------
    # CRIAR SAÍDA
    # -----------------------------------------
    try:
        row = Saida(
            sub_base=sub_base_user,
            username=username,
            entregador=entregador,
            codigo=codigo,
            servico=servico,
            status=status_val,
        )

        db.add(row)
        db.commit()
        db.refresh(row)  #  agora row.id_saida está disponível

        # -----------------------------------------
        # COBRANÇA AUTOMÁTICA (quando ignorar_coleta = true)
        # -----------------------------------------
        if ignorar:
            try:
                from models import OwnerCobrancaItem

                item = OwnerCobrancaItem(
                    sub_base=sub_base_user,
                    id_coleta=None,
                    id_saida=row.id_saida,   # <- AQUI GRAVA CORRETAMENTE
                    valor=owner.valor
                )

                db.add(item)
                db.commit()

                print(f"[COBRANÇA_SAIDA] Registrado id_saida={row.id_saida}")

            except Exception as e:
                db.rollback()  # rollback apenas da cobrança
                print(f"[COBRANÇA_SAIDA] Erro ao registrar cobrança: {e}")

    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Erro ao registrar saída: {e}")

    # retorno final
    return SaidaOut.model_validate(row)




# ---------- GET: LISTAR ----------
@router.get("/listar", response_model=List[SaidaGridItem])
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

    stmt = select(Saida).where(Saida.sub_base == sub_base_user)

    if base and base.strip() and base.lower() != "(todas)":
        stmt = stmt.where(Saida.base == base.strip())

    if de:
        stmt = stmt.where(Saida.timestamp >= datetime.combine(de, datetime.min.time()))
    if ate:
        stmt = stmt.where(Saida.timestamp <= datetime.combine(ate, datetime.max.time()))

    if entregador and entregador.strip() and entregador.lower() != "(todos)":
        stmt = stmt.where(Saida.entregador == entregador.strip())

    if status_ and status_.strip() and status_.lower() != "(todos)":
        stmt = stmt.where(Saida.status == status_.strip())

    if codigo and codigo.strip():
        stmt = stmt.where(Saida.codigo.ilike(f"%{codigo.strip()}%"))

    stmt = stmt.order_by(Saida.timestamp.desc())

    if limit:
        stmt = stmt.limit(limit)
    if offset:
        stmt = stmt.offset(offset)

    rows = db.execute(stmt).scalars().all()

    return [
        SaidaGridItem(
            id_saida=r.id_saida,
            timestamp=r.timestamp,
            username=r.username,
            entregador=r.entregador,
            codigo=r.codigo,
            servico=r.servico,
            status=r.status,
            base=r.base,
        )
        for r in rows
    ]


# ---------- PATCH: ATUALIZAR SAÍDA ----------
@router.patch("/{id_saida}", response_model=SaidaOut)
def atualizar_saida(
    id_saida: int,
    payload: SaidaUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_saida(db, sub_base_user, id_saida)

    if payload.codigo is None and payload.entregador is None and payload.status is None:
        raise HTTPException(
            422,
            {"code": "NO_FIELDS_TO_UPDATE", "message": "Nenhum campo enviado."}
        )

    try:
        if payload.codigo is not None:
            novo = payload.codigo.strip()
            if not novo:
                raise HTTPException(422, "Código não pode ser vazio.")
            if novo != obj.codigo:
                dup = db.scalars(
                    select(Saida).where(
                        Saida.sub_base == obj.sub_base,
                        Saida.codigo == novo,
                        Saida.id_saida != obj.id_saida,
                    )
                ).first()
                if dup:
                    raise HTTPException(409, f"Código '{novo}' já registrado.")
                obj.codigo = novo

        if payload.entregador is not None:
            obj.entregador = payload.entregador.strip()

        if payload.status is not None:
            obj.status = payload.status.strip()

        db.add(obj)
        db.commit()
        db.refresh(obj)

    except HTTPException:
        db.rollback()
        raise

    except Exception:
        db.rollback()
        raise HTTPException(
            500,
            {"code": "UPDATE_FAILED", "message": "Erro ao atualizar."}
        )

    return SaidaOut.model_validate(obj)


# ---------- DELETE ----------
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
