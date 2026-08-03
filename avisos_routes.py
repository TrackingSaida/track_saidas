"""
Avisos manuais da base — admin (criar/listar) e mobile motoboy (caixa/lido).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from auth import get_current_user
from db import get_db
from models import AvisoBase, AvisoDestinatario, MotoboySubBase, User
from push_notification_service import send_to_motoboy

router = APIRouter(tags=["Avisos"])

RATE_LIMIT_PER_HOUR = 20
# Comunicado fica disponível no app do motoboy apenas por este período
AVISO_TTL_HOURS = 12


def _aviso_ttl_cutoff() -> datetime:
    return datetime.utcnow() - timedelta(hours=AVISO_TTL_HOURS)


def _role(user: User) -> int:
    """Role 0 é admin global — não usar `or` (0 é falsy em Python)."""
    raw = getattr(user, "role", None)
    if raw is None or raw == "":
        return 2
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 2


def _require_admin(user: User) -> None:
    if _role(user) not in (0, 1):
        raise HTTPException(403, "Apenas admin pode enviar avisos.")


def _require_sub_base(user: User) -> str:
    sub = (getattr(user, "sub_base", None) or "").strip()
    if not sub:
        raise HTTPException(400, "sub_base não definida na sessão.")
    return sub


def _require_motoboy(user: User) -> int:
    if _role(user) != 4 or not getattr(user, "motoboy_id", None):
        raise HTTPException(403, "Acesso restrito a motoboy.")
    return int(user.motoboy_id)


class AvisoCreateIn(BaseModel):
    titulo: str = Field(..., min_length=1, max_length=120)
    mensagem: str = Field(..., min_length=1, max_length=500)
    prioridade: str = Field("normal")
    motoboy_ids: Optional[List[int]] = None
    todos_ativos: bool = False


class AvisoOut(BaseModel):
    id: int
    sub_base: str
    titulo: str
    mensagem: str
    prioridade: str
    criado_em: Optional[datetime] = None
    destinatarios_count: int = 0
    lido: Optional[bool] = None


class AvisoMotoboyOut(BaseModel):
    id: int
    titulo: str
    mensagem: str
    prioridade: str
    criado_em: Optional[datetime] = None
    lido: bool = False
    lido_em: Optional[datetime] = None


def _resolve_destinatarios(
    db: Session,
    *,
    sub_base: str,
    motoboy_ids: Optional[List[int]],
    todos_ativos: bool,
) -> List[int]:
    if todos_ativos:
        rows = db.scalars(
            select(MotoboySubBase.motoboy_id).where(
                MotoboySubBase.sub_base == sub_base,
                MotoboySubBase.ativo.is_(True),
            )
        ).all()
        return sorted({int(x) for x in rows if x is not None})
    ids = sorted({int(x) for x in (motoboy_ids or []) if x is not None})
    if not ids:
        raise HTTPException(400, "Informe motoboy_ids ou todos_ativos.")
    valid = db.scalars(
        select(MotoboySubBase.motoboy_id).where(
            MotoboySubBase.sub_base == sub_base,
            MotoboySubBase.ativo.is_(True),
            MotoboySubBase.motoboy_id.in_(ids),
        )
    ).all()
    valid_set = {int(x) for x in valid}
    missing = [i for i in ids if i not in valid_set]
    if missing:
        raise HTTPException(400, f"Motoboys inválidos para a sub_base: {missing[:5]}")
    return sorted(valid_set)


@router.post("/avisos", response_model=AvisoOut, status_code=201)
def criar_aviso(
    payload: AvisoCreateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    sub_base = _require_sub_base(current_user)
    prioridade = (payload.prioridade or "normal").strip().lower()
    if prioridade not in ("normal", "urgente"):
        raise HTTPException(400, "prioridade deve ser normal ou urgente.")

    since = datetime.utcnow() - timedelta(hours=1)
    recent = db.scalar(
        select(func.count()).select_from(AvisoBase).where(
            AvisoBase.sub_base == sub_base,
            AvisoBase.criado_por == current_user.id,
            AvisoBase.criado_em >= since,
        )
    ) or 0
    if int(recent) >= RATE_LIMIT_PER_HOUR:
        raise HTTPException(429, "Limite de avisos por hora atingido. Tente mais tarde.")

    dest_ids = _resolve_destinatarios(
        db,
        sub_base=sub_base,
        motoboy_ids=payload.motoboy_ids,
        todos_ativos=bool(payload.todos_ativos),
    )

    aviso = AvisoBase(
        sub_base=sub_base,
        criado_por=current_user.id,
        titulo=(payload.titulo or "").strip(),
        mensagem=(payload.mensagem or "").strip(),
        prioridade=prioridade,
    )
    db.add(aviso)
    db.flush()

    for mid in dest_ids:
        db.add(AvisoDestinatario(aviso_id=aviso.id, motoboy_id=mid))

    db.commit()
    db.refresh(aviso)

    tipo = "aviso_urgente" if prioridade == "urgente" else "aviso_base"
    title = aviso.titulo if prioridade != "urgente" else "Aviso urgente da base"
    body = aviso.mensagem[:180]
    for mid in dest_ids:
        try:
            send_to_motoboy(
                db,
                motoboy_id=mid,
                sub_base=sub_base,
                tipo=tipo,
                title=title,
                body=body,
                data={"aviso_id": aviso.id, "prioridade": prioridade},
            )
        except Exception:
            pass
    db.commit()

    return AvisoOut(
        id=aviso.id,
        sub_base=aviso.sub_base,
        titulo=aviso.titulo,
        mensagem=aviso.mensagem,
        prioridade=aviso.prioridade,
        criado_em=aviso.criado_em,
        destinatarios_count=len(dest_ids),
    )


@router.get("/avisos", response_model=List[AvisoOut])
def listar_avisos_admin(
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    sub_base = _require_sub_base(current_user)
    rows = db.scalars(
        select(AvisoBase)
        .where(AvisoBase.sub_base == sub_base)
        .order_by(AvisoBase.criado_em.desc())
        .limit(limit)
    ).all()
    out: List[AvisoOut] = []
    for a in rows:
        count = db.scalar(
            select(func.count()).select_from(AvisoDestinatario).where(
                AvisoDestinatario.aviso_id == a.id
            )
        ) or 0
        out.append(
            AvisoOut(
                id=a.id,
                sub_base=a.sub_base,
                titulo=a.titulo,
                mensagem=a.mensagem,
                prioridade=a.prioridade,
                criado_em=a.criado_em,
                destinatarios_count=int(count),
            )
        )
    return out


# ---------- Mobile motoboy ----------

mobile_router = APIRouter(prefix="/mobile/avisos", tags=["Mobile Avisos"])


@mobile_router.get("", response_model=List[AvisoMotoboyOut])
def listar_avisos_motoboy(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    motoboy_id = _require_motoboy(current_user)
    sub_base = _require_sub_base(current_user)
    rows = db.execute(
        select(AvisoBase, AvisoDestinatario)
        .join(AvisoDestinatario, AvisoDestinatario.aviso_id == AvisoBase.id)
        .where(
            AvisoBase.sub_base == sub_base,
            AvisoDestinatario.motoboy_id == motoboy_id,
            AvisoBase.criado_em >= _aviso_ttl_cutoff(),
        )
        .order_by(AvisoBase.criado_em.desc())
        .limit(100)
    ).all()
    return [
        AvisoMotoboyOut(
            id=a.id,
            titulo=a.titulo,
            mensagem=a.mensagem,
            prioridade=a.prioridade,
            criado_em=a.criado_em,
            lido=d.lido_em is not None,
            lido_em=d.lido_em,
        )
        for a, d in rows
    ]


@mobile_router.get("/urgentes-pendentes", response_model=List[AvisoMotoboyOut])
def listar_urgentes_pendentes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    motoboy_id = _require_motoboy(current_user)
    sub_base = _require_sub_base(current_user)
    rows = db.execute(
        select(AvisoBase, AvisoDestinatario)
        .join(AvisoDestinatario, AvisoDestinatario.aviso_id == AvisoBase.id)
        .where(
            AvisoBase.sub_base == sub_base,
            AvisoDestinatario.motoboy_id == motoboy_id,
            AvisoBase.prioridade == "urgente",
            AvisoDestinatario.lido_em.is_(None),
            AvisoBase.criado_em >= _aviso_ttl_cutoff(),
        )
        .order_by(AvisoBase.criado_em.asc())
    ).all()
    return [
        AvisoMotoboyOut(
            id=a.id,
            titulo=a.titulo,
            mensagem=a.mensagem,
            prioridade=a.prioridade,
            criado_em=a.criado_em,
            lido=False,
            lido_em=None,
        )
        for a, d in rows
    ]


@mobile_router.get("/{aviso_id}", response_model=AvisoMotoboyOut)
def obter_aviso_motoboy(
    aviso_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    motoboy_id = _require_motoboy(current_user)
    sub_base = _require_sub_base(current_user)
    row = db.execute(
        select(AvisoBase, AvisoDestinatario)
        .join(AvisoDestinatario, AvisoDestinatario.aviso_id == AvisoBase.id)
        .where(
            AvisoBase.id == aviso_id,
            AvisoBase.sub_base == sub_base,
            AvisoDestinatario.motoboy_id == motoboy_id,
        )
    ).first()
    if not row:
        raise HTTPException(404, "Aviso não encontrado.")
    a, d = row
    if a.criado_em and a.criado_em < _aviso_ttl_cutoff():
        raise HTTPException(410, "Este aviso expirou (disponível por 12 horas).")
    return AvisoMotoboyOut(
        id=a.id,
        titulo=a.titulo,
        mensagem=a.mensagem,
        prioridade=a.prioridade,
        criado_em=a.criado_em,
        lido=d.lido_em is not None,
        lido_em=d.lido_em,
    )


@mobile_router.post("/{aviso_id}/lido")
def marcar_aviso_lido(
    aviso_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    motoboy_id = _require_motoboy(current_user)
    sub_base = _require_sub_base(current_user)
    dest = db.scalar(
        select(AvisoDestinatario)
        .join(AvisoBase, AvisoBase.id == AvisoDestinatario.aviso_id)
        .where(
            AvisoDestinatario.aviso_id == aviso_id,
            AvisoDestinatario.motoboy_id == motoboy_id,
            AvisoBase.sub_base == sub_base,
        )
    )
    if not dest:
        raise HTTPException(404, "Aviso não encontrado.")
    if dest.lido_em is None:
        dest.lido_em = datetime.utcnow()
        db.commit()
    return {"ok": True}
