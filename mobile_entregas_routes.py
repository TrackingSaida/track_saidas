"""
Rotas do App Motoboy (mobile).
Prefixo: /mobile
Requer JWT de motoboy (role=4, motoboy_id no token).
"""
from __future__ import annotations

from datetime import datetime, date
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import (
    User,
    Saida,
    SaidaDetail,
    Motoboy,
    MotoboySubBase,
    MotivoAusencia,
    SaidaHistorico,
)
from saidas_routes import (
    STATUS_SAIU_PARA_ENTREGA,
    STATUS_EM_ROTA,
    STATUS_ENTREGUE,
    STATUS_AUSENTE,
    STATUS_CANCELADO,
)

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


# ============================================================
# Schemas
# ============================================================
class EntregaListItem(BaseModel):
    id_saida: int
    codigo: Optional[str]
    status: str
    exibicao: str  # "Pendente" | "Entregue" | "Ausente"
    cliente: Optional[str] = None
    bairro: Optional[str] = None
    endereco: Optional[str] = None
    contato: Optional[str] = None
    data: Optional[date] = None
    data_hora_entrega: Optional[datetime] = None


class ScanBody(BaseModel):
    codigo: str = Field(min_length=1)


class AusenteBody(BaseModel):
    motivo_id: int
    observacao: Optional[str] = None


class MotivoAusenciaOut(BaseModel):
    id: int
    descricao: str


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


def _saida_to_item(s: Saida, detail: Optional[SaidaDetail]) -> dict:
    endereco = None
    if detail and (detail.dest_rua or detail.dest_numero):
        parts = [p for p in [detail.dest_rua, detail.dest_numero, detail.dest_complemento] if p]
        endereco = ", ".join(parts) if parts else None
    return {
        "id_saida": s.id_saida,
        "codigo": s.codigo,
        "status": s.status or "",
        "exibicao": _status_exibicao(s.status),
        "cliente": detail.dest_nome if detail else None,
        "bairro": detail.dest_bairro if detail else None,
        "endereco": endereco,
        "contato": detail.dest_contato if detail else None,
        "data": s.data,
        "data_hora_entrega": s.data_hora_entrega,
    }


# ============================================================
# GET /mobile/entregas
# ============================================================
@router.get("/entregas", response_model=List[EntregaListItem])
def listar_entregas(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Lista entregas do motoboy. status=pendente | finalizadas | ausentes."""
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    q = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.motoboy_id == motoboy_id,
    )
    if status == "pendente":
        q = q.where(Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]))
    elif status == "finalizadas":
        q = q.where(Saida.status == STATUS_ENTREGUE)
    elif status == "ausentes":
        q = q.where(Saida.status == STATUS_AUSENTE)
    q = q.order_by(Saida.data.desc(), Saida.timestamp.desc())

    rows = db.scalars(q).all()
    out = []
    for s in rows:
        detail = _get_detail_for_saida(db, s.id_saida)
        out.append(_saida_to_item(s, detail))
    return out


# ============================================================
# GET /mobile/entregas/resumo
# ============================================================
@router.get("/entregas/resumo")
def resumo_entregas(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Contadores: pendentes, finalizadas_hoje."""
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    hoje = date.today()
    pendentes = db.scalar(
        select(func.count(Saida.id_saida)).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]),
        )
    ) or 0
    finalizadas_hoje = db.scalar(
        select(func.count(Saida.id_saida)).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status == STATUS_ENTREGUE,
            func.date(Saida.data_hora_entrega) == hoje,
        )
    ) or 0
    tem_saiu_para_entrega = db.scalar(
        select(func.count(Saida.id_saida)).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status == STATUS_SAIU_PARA_ENTREGA,
        )
    ) or 0

    return {
        "pendentes": pendentes,
        "finalizadas_hoje": finalizadas_hoje,
        "pode_iniciar_rota": tem_saiu_para_entrega > 0,
    }


# ============================================================
# POST /mobile/iniciar-rota
# ============================================================
@router.post("/iniciar-rota")
def iniciar_rota(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Atualiza todas SAIU_PARA_ENTREGA do motoboy para EM_ROTA."""
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

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
    db.commit()
    return {"atualizados": len(rows)}


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
# POST /mobile/entrega/{id}/entregue
# ============================================================
@router.post("/entrega/{id_saida}/entregue")
def marcar_entregue(
    id_saida: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Marca entrega como ENTREGUE e registra data_hora_entrega."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    s.status = STATUS_ENTREGUE
    s.data_hora_entrega = datetime.utcnow()
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
    """Marca entrega como AUSENTE com motivo (e observação se motivo 'Outro')."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
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
    db.commit()
    return {"ok": True, "id_saida": id_saida}


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
# POST /mobile/scan
# ============================================================
@router.post("/scan")
def scan_codigo(
    body: ScanBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """
    Escaneia código: se saída não tem motoboy -> atribui ao logado e retorna 200.
    Se já tem outro motoboy -> retorna 409 com conflito: true, motoboy_atual.
    """
    codigo = body.codigo.strip()
    sub_base = user.sub_base
    motoboy_id = user.motoboy_id
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    saida = db.scalar(
        select(Saida).where(
            Saida.codigo == codigo,
            Saida.sub_base == sub_base,
        )
    )
    if not saida:
        raise HTTPException(status_code=404, detail="Código não encontrado.")

    if saida.motoboy_id is None:
        saida.motoboy_id = motoboy_id
        saida.status = STATUS_SAIU_PARA_ENTREGA
        db.commit()
        db.refresh(saida)
        detail = _get_detail_for_saida(db, saida.id_saida)
        return {"ok": True, "conflito": False, "entrega": _saida_to_item(saida, detail)}

    if saida.motoboy_id == motoboy_id:
        detail = _get_detail_for_saida(db, saida.id_saida)
        return {"ok": True, "conflito": False, "entrega": _saida_to_item(saida, detail)}

    motoboy_atual = db.get(Motoboy, saida.motoboy_id)
    nome_atual = "Motoboy"
    if motoboy_atual and motoboy_atual.user_id:
        from models import User as UserModel
        u = db.get(UserModel, motoboy_atual.user_id)
        if u:
            nome_atual = f"{u.nome or ''} {u.sobrenome or ''}".strip() or u.username or nome_atual
    return JSONResponse(
        status_code=409,
        content={
            "conflito": True,
            "motoboy_atual": nome_atual,
            "id_saida": saida.id_saida,
        },
    )


# ============================================================
# POST /mobile/entrega/{id}/assumir
# ============================================================
@router.post("/entrega/{id_saida}/assumir")
def assumir_entrega(
    id_saida: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Reatribui a entrega para o motoboy logado (após conflito no scan)."""
    s = db.get(Saida, id_saida)
    if not s or s.sub_base != user.sub_base:
        raise HTTPException(status_code=404, detail="Entrega não encontrada.")
    if s.motoboy_id == user.motoboy_id:
        return {"ok": True, "id_saida": id_saida}

    antigo = s.motoboy_id
    s.motoboy_id = user.motoboy_id
    s.status = STATUS_SAIU_PARA_ENTREGA
    db.add(
        SaidaHistorico(
            id_saida=id_saida,
            evento="reatribuicao",
            motoboy_id_anterior=antigo,
            motoboy_id_novo=user.motoboy_id,
            user_id=user.id,
        )
    )
    db.commit()
    return {"ok": True, "id_saida": id_saida}
