"""
Avisos manuais da base — admin (criar/listar/editar/reenviar) e mobile motoboy (caixa/lido).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from auth import get_current_user, ensure_motoboy_session
from avisos_service import MENSAGEM_MAX_LEN, normalize_prioridade, sanitize_mensagem
from db import get_db
from models import AvisoBase, AvisoDestinatario, MotoboySubBase, User
from push_notification_service import send_to_motoboy

router = APIRouter(tags=["Avisos"])

RATE_LIMIT_PER_HOUR = 20
# Comunicado fica disponível no app do motoboy apenas por este período
AVISO_TTL_HOURS = 12

# Reexport para testes legados / imports externos
_sanitize_mensagem = sanitize_mensagem
_normalize_prioridade = normalize_prioridade


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


def get_current_motoboy_avisos(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Hidrata motoboy_id/sub_base — JWT staff legado (role=4 sem motoboy_id) deixa de quebrar avisos."""
    return ensure_motoboy_session(db, user)


class AvisoCreateIn(BaseModel):
    titulo: str = Field(..., min_length=1, max_length=120)
    mensagem: str = Field(..., min_length=1, max_length=MENSAGEM_MAX_LEN)
    prioridade: str = Field("normal")
    motoboy_ids: Optional[List[int]] = None
    todos_ativos: bool = False

    @field_validator("mensagem")
    @classmethod
    def _val_mensagem(cls, v: str) -> str:
        return sanitize_mensagem(v)

    @field_validator("titulo")
    @classmethod
    def _val_titulo(cls, v: str) -> str:
        t = (v or "").strip()
        if not t:
            raise ValueError("titulo obrigatório")
        return t


class AvisoUpdateIn(BaseModel):
    titulo: Optional[str] = Field(None, min_length=1, max_length=120)
    mensagem: Optional[str] = Field(None, min_length=1, max_length=MENSAGEM_MAX_LEN)
    prioridade: Optional[str] = None

    @field_validator("mensagem")
    @classmethod
    def _val_mensagem(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return sanitize_mensagem(v)

    @field_validator("titulo")
    @classmethod
    def _val_titulo(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        t = v.strip()
        if not t:
            raise ValueError("titulo obrigatório")
        return t


class AvisoOut(BaseModel):
    id: int
    sub_base: str
    titulo: str
    mensagem: str
    prioridade: str
    criado_em: Optional[datetime] = None
    destinatarios_count: int = 0
    lido: Optional[bool] = None
    motoboy_ids: Optional[List[int]] = None


class AvisoMotoboyOut(BaseModel):
    id: int
    titulo: str
    mensagem: str
    prioridade: str
    criado_em: Optional[datetime] = None
    lido: bool = False
    lido_em: Optional[datetime] = None


def _check_rate_limit(db: Session, *, sub_base: str, user_id: int) -> None:
    since = datetime.utcnow() - timedelta(hours=1)
    recent = db.scalar(
        select(func.count()).select_from(AvisoBase).where(
            AvisoBase.sub_base == sub_base,
            AvisoBase.criado_por == user_id,
            AvisoBase.criado_em >= since,
        )
    ) or 0
    if int(recent) >= RATE_LIMIT_PER_HOUR:
        raise HTTPException(429, "Limite de avisos por hora atingido. Tente mais tarde.")


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


def _active_destinatarios_of_aviso(
    db: Session,
    *,
    aviso_id: int,
    sub_base: str,
) -> List[int]:
    """IDs do aviso original que ainda estão ativos na sub_base."""
    original_ids = list(
        db.scalars(
            select(AvisoDestinatario.motoboy_id).where(AvisoDestinatario.aviso_id == aviso_id)
        ).all()
    )
    if not original_ids:
        return []
    valid = db.scalars(
        select(MotoboySubBase.motoboy_id).where(
            MotoboySubBase.sub_base == sub_base,
            MotoboySubBase.ativo.is_(True),
            MotoboySubBase.motoboy_id.in_([int(x) for x in original_ids]),
        )
    ).all()
    return sorted({int(x) for x in valid if x is not None})


def _send_aviso_pushes(
    db: Session,
    *,
    aviso: AvisoBase,
    dest_ids: List[int],
) -> None:
    tipo = "aviso_urgente" if aviso.prioridade == "urgente" else "aviso_base"
    title = aviso.titulo if aviso.prioridade != "urgente" else "Aviso urgente da base"
    body = (aviso.mensagem or "")[:180]
    for mid in dest_ids:
        try:
            send_to_motoboy(
                db,
                motoboy_id=mid,
                sub_base=aviso.sub_base,
                tipo=tipo,
                title=title,
                body=body,
                data={
                    "aviso_id": str(aviso.id),
                    "prioridade": aviso.prioridade,
                    "titulo": aviso.titulo or "",
                    "mensagem": aviso.mensagem or "",
                },
            )
        except Exception:
            pass


def _get_aviso_admin(db: Session, *, aviso_id: int, sub_base: str) -> AvisoBase:
    aviso = db.scalar(
        select(AvisoBase).where(AvisoBase.id == aviso_id, AvisoBase.sub_base == sub_base)
    )
    if not aviso:
        raise HTTPException(404, "Aviso não encontrado.")
    return aviso


def _aviso_out(db: Session, aviso: AvisoBase, *, with_ids: bool = False) -> AvisoOut:
    dest_ids = list(
        db.scalars(
            select(AvisoDestinatario.motoboy_id).where(AvisoDestinatario.aviso_id == aviso.id)
        ).all()
    )
    ids = [int(x) for x in dest_ids if x is not None]
    return AvisoOut(
        id=aviso.id,
        sub_base=aviso.sub_base,
        titulo=aviso.titulo,
        mensagem=aviso.mensagem,
        prioridade=aviso.prioridade,
        criado_em=aviso.criado_em,
        destinatarios_count=len(ids),
        motoboy_ids=ids if with_ids else None,
    )


def _create_aviso_record(
    db: Session,
    *,
    sub_base: str,
    user_id: int,
    titulo: str,
    mensagem: str,
    prioridade: str,
    dest_ids: List[int],
) -> AvisoBase:
    aviso = AvisoBase(
        sub_base=sub_base,
        criado_por=user_id,
        titulo=titulo,
        mensagem=mensagem,
        prioridade=prioridade,
    )
    db.add(aviso)
    db.flush()
    for mid in dest_ids:
        db.add(AvisoDestinatario(aviso_id=aviso.id, motoboy_id=mid))
    db.commit()
    db.refresh(aviso)
    _send_aviso_pushes(db, aviso=aviso, dest_ids=dest_ids)
    db.commit()
    return aviso


@router.post("/avisos", response_model=AvisoOut, status_code=201)
def criar_aviso(
    payload: AvisoCreateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    sub_base = _require_sub_base(current_user)
    prioridade = normalize_prioridade(payload.prioridade)
    _check_rate_limit(db, sub_base=sub_base, user_id=current_user.id)

    dest_ids = _resolve_destinatarios(
        db,
        sub_base=sub_base,
        motoboy_ids=payload.motoboy_ids,
        todos_ativos=bool(payload.todos_ativos),
    )

    aviso = _create_aviso_record(
        db,
        sub_base=sub_base,
        user_id=current_user.id,
        titulo=payload.titulo,
        mensagem=payload.mensagem,
        prioridade=prioridade,
        dest_ids=dest_ids,
    )
    return _aviso_out(db, aviso)


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
    return [_aviso_out(db, a) for a in rows]


@router.get("/avisos/{aviso_id}", response_model=AvisoOut)
def obter_aviso_admin(
    aviso_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    sub_base = _require_sub_base(current_user)
    aviso = _get_aviso_admin(db, aviso_id=aviso_id, sub_base=sub_base)
    return _aviso_out(db, aviso, with_ids=True)


@router.patch("/avisos/{aviso_id}", response_model=AvisoOut)
def editar_aviso(
    aviso_id: int,
    payload: AvisoUpdateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Atualiza título/mensagem/prioridade sem disparar push."""
    _require_admin(current_user)
    sub_base = _require_sub_base(current_user)
    aviso = _get_aviso_admin(db, aviso_id=aviso_id, sub_base=sub_base)

    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(400, "Nenhum campo para atualizar.")

    if "titulo" in data and data["titulo"] is not None:
        aviso.titulo = data["titulo"]
    if "mensagem" in data and data["mensagem"] is not None:
        aviso.mensagem = data["mensagem"]
    if "prioridade" in data and data["prioridade"] is not None:
        aviso.prioridade = normalize_prioridade(data["prioridade"])

    db.commit()
    db.refresh(aviso)
    return _aviso_out(db, aviso, with_ids=True)


@router.post("/avisos/{aviso_id}/reenviar", response_model=AvisoOut, status_code=201)
def reenviar_aviso(
    aviso_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cria aviso novo com o texto já salvo, só para motoboys ainda ativos do original."""
    _require_admin(current_user)
    sub_base = _require_sub_base(current_user)
    original = _get_aviso_admin(db, aviso_id=aviso_id, sub_base=sub_base)
    _check_rate_limit(db, sub_base=sub_base, user_id=current_user.id)

    dest_ids = _active_destinatarios_of_aviso(db, aviso_id=original.id, sub_base=sub_base)
    if not dest_ids:
        raise HTTPException(400, "Nenhum motoboy ativo restante entre os destinatários originais.")

    aviso = _create_aviso_record(
        db,
        sub_base=sub_base,
        user_id=current_user.id,
        titulo=original.titulo,
        mensagem=original.mensagem,
        prioridade=original.prioridade,
        dest_ids=dest_ids,
    )
    return _aviso_out(db, aviso, with_ids=True)


# ---------- Mobile motoboy ----------

mobile_router = APIRouter(prefix="/mobile/avisos", tags=["Mobile Avisos"])


@mobile_router.get("", response_model=List[AvisoMotoboyOut])
def listar_avisos_motoboy(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_motoboy_avisos),
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
    current_user: User = Depends(get_current_motoboy_avisos),
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
    current_user: User = Depends(get_current_motoboy_avisos),
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
    current_user: User = Depends(get_current_motoboy_avisos),
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
