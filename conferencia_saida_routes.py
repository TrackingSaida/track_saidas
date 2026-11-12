"""Endpoints de Conferência de Saída (staff)."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from auth import get_current_user
from conferencia_saida_service import (
    STATUS_CONFERIDA,
    STATUS_PENDENTE,
    STATUS_RECONFERIR,
    carregar_nomes_motoboy,
    conferir_saida,
    contar_saidas_por_motoboy_dia,
    contar_totais_e_novos_por_motoboy_dia,
    listar_saidas_motoboy_dia,
    listar_saidas_novas_apos_conferencia,
    owner_conferencia_habilitada,
    resumo_novos_pacotes,
    somar_servicos_saidas,
    upsert_conferencia_dia,
)
from db import get_db
from models import ConferenciaSaida, Saida, SaidaHistorico, SaidaSessao, SaidaSessaoItem, User
from saida_sessao_service import (
    ESTADO_ABERTA,
    finalizar_sessao,
    fluxo_confirmacao_ativo,
    obter_sessao_aberta,
)

router = APIRouter(prefix="/conferencias-saida", tags=["ConferenciasSaida"])

AbaTipo = Literal["pendente", "reconferir", "conferida"]


class ConferenciaItemOut(BaseModel):
    id: int
    sessao_id: Optional[int] = None
    motoboy_id: int
    motoboy_nome: str
    data_ref: date
    status: str
    qtd_no_momento: Optional[int] = None
    conferido_em: Optional[str] = None
    ultima_abertura_em: Optional[str] = None
    # Pacotes que entraram após a última conferência (só relevante em reconferir).
    novos_qtd: Optional[int] = None


class ConferenciaListOut(BaseModel):
    items: List[ConferenciaItemOut]
    total: int


class PacoteNovoOut(BaseModel):
    codigo: str
    servico: str


class ConferenciaDetalheOut(BaseModel):
    sessao_id: Optional[int] = None
    motoboy_id: int
    motoboy_nome: str
    data_ref: date
    status: str
    sum_shopee: int
    sum_mercado: int
    sum_avulso: int
    total: int
    qtd_no_momento: Optional[int] = None
    conferido_em: Optional[str] = None
    novos_qtd: int = 0
    novos_shopee: int = 0
    novos_mercado: int = 0
    novos_avulso: int = 0
    novos_pacotes: List[PacoteNovoOut] = Field(default_factory=list)


class ConferirBody(BaseModel):
    data_ref: date = Field(...)


class ConfirmarLeituraBody(BaseModel):
    motoboy_id: int = Field(..., ge=1)
    data_ref: date = Field(...)
    qtd: Optional[int] = Field(None, ge=0)
    sessao_id: Optional[int] = Field(None, ge=1)


class ConfirmarLeituraOut(BaseModel):
    motoboy_id: int
    motoboy_nome: str
    data_ref: date
    status: str
    qtd_no_momento: int
    sum_shopee: int
    sum_mercado: int
    sum_avulso: int
    total: int
    virou_reconferir: bool = False
    sessao_id: Optional[int] = None
    sessao_status: Optional[str] = None
    sessao_total: Optional[int] = None
    fluxo_confirmacao_ativo: bool = False


class SessaoAbertaOut(BaseModel):
    fluxo_confirmacao_ativo: bool
    sessao_id: Optional[int] = None
    sessao_status: Optional[str] = None
    sessao_total: int = 0
    saida_ids: List[int] = Field(default_factory=list)


def _require_staff(user: User) -> None:
    role = int(getattr(user, "role", 0) or 0)
    if role not in (0, 1, 2, 3):
        raise HTTPException(status_code=403, detail="Acesso restrito a admin/operador.")


def _require_flag(db: Session, user: User, sub_base: str) -> None:
    if not owner_conferencia_habilitada(db, sub_base, user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CONFERENCIA_DESABILITADA",
                "message": "Conferência de saída não está habilitada para esta base.",
            },
        )


def _detalhe_out(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    data_ref: date,
    row: ConferenciaSaida,
) -> ConferenciaDetalheOut:
    saidas = listar_saidas_motoboy_dia(
        db, sub_base=sub_base, motoboy_id=motoboy_id, data_ref=data_ref
    )
    shopee, ml, avulso = somar_servicos_saidas(saidas)
    nome = carregar_nomes_motoboy(db, [motoboy_id]).get(motoboy_id, f"Motoboy {motoboy_id}")
    total_vivo = shopee + ml + avulso

    novos_payload = {
        "novos_qtd": 0,
        "novos_shopee": 0,
        "novos_mercado": 0,
        "novos_avulso": 0,
        "novos_pacotes": [],
    }
    if row.status == STATUS_RECONFERIR:
        novos = listar_saidas_novas_apos_conferencia(
            db, sub_base=sub_base, motoboy_id=motoboy_id, data_ref=data_ref
        )
        novos_payload = resumo_novos_pacotes(novos)

    return ConferenciaDetalheOut(
        sessao_id=int(row.sessao_id) if row.sessao_id is not None else None,
        motoboy_id=motoboy_id,
        motoboy_nome=nome,
        data_ref=data_ref,
        status=row.status,
        sum_shopee=shopee,
        sum_mercado=ml,
        sum_avulso=avulso,
        total=total_vivo,
        qtd_no_momento=total_vivo,
        conferido_em=row.conferido_em.isoformat() if row.conferido_em else None,
        novos_qtd=int(novos_payload["novos_qtd"]),
        novos_shopee=int(novos_payload["novos_shopee"]),
        novos_mercado=int(novos_payload["novos_mercado"]),
        novos_avulso=int(novos_payload["novos_avulso"]),
        novos_pacotes=[PacoteNovoOut(**p) for p in novos_payload["novos_pacotes"]],
    )


def _saidas_operacionais_sessao(db: Session, sessao: SaidaSessao) -> List[Saida]:
    return list(
        db.scalars(
            select(Saida)
            .join(SaidaSessaoItem, SaidaSessaoItem.id_saida == Saida.id_saida)
            .where(
                SaidaSessaoItem.sessao_id == sessao.id,
                SaidaSessaoItem.ativo_operacional.is_(True),
                Saida.sub_base == sessao.sub_base,
                Saida.motoboy_id == sessao.motoboy_id,
            )
            .order_by(Saida.codigo, Saida.id_saida)
        ).all()
    )


def _detalhe_sessao_out(
    db: Session, *, sessao: SaidaSessao, row: ConferenciaSaida
) -> ConferenciaDetalheOut:
    saidas = _saidas_operacionais_sessao(db, sessao)
    shopee, ml, avulso = somar_servicos_saidas(saidas)
    nome = carregar_nomes_motoboy(db, [int(sessao.motoboy_id)]).get(
        int(sessao.motoboy_id), f"Motoboy {sessao.motoboy_id}"
    )
    return ConferenciaDetalheOut(
        sessao_id=int(sessao.id),
        motoboy_id=int(sessao.motoboy_id),
        motoboy_nome=nome,
        data_ref=sessao.data_ref,
        status=row.status,
        sum_shopee=shopee,
        sum_mercado=ml,
        sum_avulso=avulso,
        total=len(saidas),
        qtd_no_momento=len(saidas),
        conferido_em=row.conferido_em.isoformat() if row.conferido_em else None,
    )


@router.post("/confirmar-leitura", response_model=ConfirmarLeituraOut)
def confirmar_leitura(
    body: ConfirmarLeituraBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Staff confirma a leitura do dia operacional do motoboy.
    Cria/atualiza conferencia_saida (Pendente ou Reconferir) sem exigir iniciar rota.
    """
    _require_staff(current_user)
    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(403, "Sub-base não definida.")
    _require_flag(db, current_user, sub_base)

    fluxo_sessao = fluxo_confirmacao_ativo(db, sub_base)
    if fluxo_sessao:
        try:
            sessao = finalizar_sessao(
                db,
                sub_base=sub_base,
                motoboy_id=int(body.motoboy_id),
                user_id=int(current_user.id),
                sessao_id=body.sessao_id,
            )
            if sessao.estado != "FINALIZADA":
                db.commit()
                raise HTTPException(
                    422,
                    detail={"code": "SEM_LEITURAS", "message": "A sessão não possui pacotes válidos."},
                )
            row = db.scalar(
                select(ConferenciaSaida).where(ConferenciaSaida.sessao_id == sessao.id)
            )
            if row is None:
                raise HTTPException(500, "Conferência da sessão não foi criada.")
            db.commit()
            db.refresh(sessao)
            db.refresh(row)
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(500, f"Erro ao finalizar sessão de leitura: {e}")

        nome = carregar_nomes_motoboy(db, [int(body.motoboy_id)]).get(
            int(body.motoboy_id), f"Motoboy {body.motoboy_id}"
        )
        return ConfirmarLeituraOut(
            motoboy_id=int(body.motoboy_id),
            motoboy_nome=nome,
            data_ref=sessao.data_ref,
            status=row.status,
            qtd_no_momento=int(sessao.total_snapshot or 0),
            sum_shopee=int(sessao.total_shopee_snapshot or 0),
            sum_mercado=int(sessao.total_ml_snapshot or 0),
            sum_avulso=int(sessao.total_avulso_snapshot or 0),
            total=int(sessao.total_snapshot or 0),
            virou_reconferir=False,
            sessao_id=int(sessao.id),
            sessao_status=sessao.estado,
            sessao_total=int(sessao.total_snapshot or 0),
            fluxo_confirmacao_ativo=True,
        )

    saidas = listar_saidas_motoboy_dia(
        db,
        sub_base=sub_base,
        motoboy_id=int(body.motoboy_id),
        data_ref=body.data_ref,
    )
    qtd = len(saidas)
    if body.qtd is not None and int(body.qtd) > qtd:
        # Client pode informar qtd da sessão; nunca ultrapassa o operacional do dia.
        qtd = max(qtd, 0)
    if qtd <= 0:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SEM_LEITURAS",
                "message": "Não há leituras operacionais deste motoboy para a data informada.",
            },
        )

    try:
        row, virou_reconferir = upsert_conferencia_dia(
            db,
            sub_base=sub_base,
            motoboy_id=int(body.motoboy_id),
            data_ref=body.data_ref,
            qtd=qtd,
        )
        if row is None:
            raise HTTPException(422, "Não foi possível criar a conferência.")
        db.commit()
        db.refresh(row)
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Erro ao confirmar leitura: {e}")

    if virou_reconferir:
        try:
            from push_notification_service import send_to_staff_sub_base

            nomes = carregar_nomes_motoboy(db, [int(body.motoboy_id)])
            nome = nomes.get(int(body.motoboy_id)) or f"Motoboy {body.motoboy_id}"
            send_to_staff_sub_base(
                db,
                sub_base=sub_base,
                tipo="reconferir_saida",
                title="Reconferência necessária",
                body=f"Reconferir do {nome}",
                data={
                    "motoboy_id": int(body.motoboy_id),
                    "data_ref": body.data_ref.isoformat(),
                    "sub_base": sub_base,
                },
            )
        except Exception:
            pass

    shopee, ml, avulso = somar_servicos_saidas(saidas)
    nome = carregar_nomes_motoboy(db, [int(body.motoboy_id)]).get(
        int(body.motoboy_id), f"Motoboy {body.motoboy_id}"
    )
    return ConfirmarLeituraOut(
        motoboy_id=int(body.motoboy_id),
        motoboy_nome=nome,
        data_ref=body.data_ref,
        status=row.status,
        qtd_no_momento=int(row.qtd_no_momento or qtd),
        sum_shopee=shopee,
        sum_mercado=ml,
        sum_avulso=avulso,
        total=shopee + ml + avulso,
        virou_reconferir=bool(virou_reconferir),
        fluxo_confirmacao_ativo=False,
    )


@router.get("/sessao-aberta/{motoboy_id}", response_model=SessaoAbertaOut)
def recuperar_sessao_aberta(
    motoboy_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_staff(current_user)
    sub_base = (current_user.sub_base or "").strip()
    ativo = fluxo_confirmacao_ativo(db, sub_base)
    if not ativo:
        return SessaoAbertaOut(fluxo_confirmacao_ativo=False)
    sessao = obter_sessao_aberta(db, sub_base=sub_base, motoboy_id=motoboy_id)
    if sessao is None:
        return SessaoAbertaOut(fluxo_confirmacao_ativo=True)
    saida_ids = list(
        db.scalars(
            select(SaidaSessaoItem.id_saida).where(
                SaidaSessaoItem.sessao_id == sessao.id,
                SaidaSessaoItem.ativo_operacional.is_(True),
            )
        ).all()
    )
    return SessaoAbertaOut(
        fluxo_confirmacao_ativo=True,
        sessao_id=int(sessao.id),
        sessao_status=sessao.estado,
        sessao_total=len(saida_ids),
        saida_ids=[int(x) for x in saida_ids],
    )


@router.get("/sessoes/{sessao_id}", response_model=ConferenciaDetalheOut)
def detalhe_conferencia_sessao(
    sessao_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_staff(current_user)
    sub_base = (current_user.sub_base or "").strip()
    _require_flag(db, current_user, sub_base)
    sessao = db.scalar(
        select(SaidaSessao).where(SaidaSessao.id == sessao_id, SaidaSessao.sub_base == sub_base)
    )
    row = db.scalar(
        select(ConferenciaSaida).where(
            ConferenciaSaida.sessao_id == sessao_id,
            ConferenciaSaida.sub_base == sub_base,
        )
    )
    if sessao is None or row is None:
        raise HTTPException(404, "Conferência da sessão não encontrada.")
    return _detalhe_sessao_out(db, sessao=sessao, row=row)


@router.post("/sessoes/{sessao_id}/conferir", response_model=ConferenciaDetalheOut)
def conferir_sessao(
    sessao_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_staff(current_user)
    sub_base = (current_user.sub_base or "").strip()
    _require_flag(db, current_user, sub_base)
    sessao = db.scalar(
        select(SaidaSessao)
        .where(SaidaSessao.id == sessao_id, SaidaSessao.sub_base == sub_base)
        .with_for_update()
    )
    row = db.scalar(
        select(ConferenciaSaida)
        .where(ConferenciaSaida.sessao_id == sessao_id, ConferenciaSaida.sub_base == sub_base)
        .with_for_update()
    )
    if sessao is None or row is None:
        raise HTTPException(404, "Conferência da sessão não encontrada.")
    now = datetime.utcnow()
    saidas = _saidas_operacionais_sessao(db, sessao)
    evento = "saida_reconferida" if row.status == STATUS_RECONFERIR else "saida_conferida"
    for saida in saidas:
        db.add(
            SaidaHistorico(
                id_saida=saida.id_saida,
                evento=evento,
                status_anterior=saida.status,
                status_novo=saida.status,
                motoboy_id_anterior=saida.motoboy_id,
                motoboy_id_novo=saida.motoboy_id,
                user_id=getattr(current_user, "id", None),
                payload=json.dumps({"sessao_id": sessao_id}),
            )
        )
    row.status = STATUS_CONFERIDA
    row.conferido_em = now
    row.conferido_por = getattr(current_user, "id", None)
    row.qtd_no_momento = len(saidas)
    db.commit()
    db.refresh(row)
    return _detalhe_sessao_out(db, sessao=sessao, row=row)


@router.get("", response_model=ConferenciaListOut)
def listar_conferencias(
    data_inicio: date = Query(...),
    data_fim: date = Query(...),
    aba: AbaTipo = Query("pendente"),
    motoboy_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_staff(current_user)
    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(403, "Sub-base não definida.")
    _require_flag(db, current_user, sub_base)

    if data_inicio > data_fim:
        raise HTTPException(400, "data_inicio deve ser anterior a data_fim.")

    status_map = {
        "pendente": STATUS_PENDENTE,
        "reconferir": STATUS_RECONFERIR,
        "conferida": STATUS_CONFERIDA,
    }
    status = status_map[aba]

    q = select(ConferenciaSaida).where(
        ConferenciaSaida.sub_base == sub_base,
        ConferenciaSaida.data_ref >= data_inicio,
        ConferenciaSaida.data_ref <= data_fim,
        ConferenciaSaida.status == status,
    )
    if motoboy_id is not None:
        q = q.where(ConferenciaSaida.motoboy_id == motoboy_id)

    rows = list(db.scalars(q).all())
    nomes = carregar_nomes_motoboy(db, [int(r.motoboy_id) for r in rows])
    chaves = [(int(r.motoboy_id), r.data_ref) for r in rows]
    qtd_map: dict = {}
    novos_map: dict = {}
    qtd_sessao_map: dict[int, int] = {}
    if rows:
        sessao_ids = [int(r.sessao_id) for r in rows if r.sessao_id is not None]
        if sessao_ids:
            qtd_sessao_map = {
                int(sessao_id): int(total)
                for sessao_id, total in db.execute(
                    select(SaidaSessaoItem.sessao_id, func.count(Saida.id_saida))
                    .join(SaidaSessao, SaidaSessao.id == SaidaSessaoItem.sessao_id)
                    .join(Saida, Saida.id_saida == SaidaSessaoItem.id_saida)
                    .where(
                        SaidaSessaoItem.sessao_id.in_(sessao_ids),
                        SaidaSessaoItem.ativo_operacional.is_(True),
                        Saida.sub_base == SaidaSessao.sub_base,
                        Saida.motoboy_id == SaidaSessao.motoboy_id,
                    )
                    .group_by(SaidaSessaoItem.sessao_id)
                ).all()
            }
        if status == STATUS_RECONFERIR:
            qtd_map, novos_map = contar_totais_e_novos_por_motoboy_dia(
                db, sub_base=sub_base, chaves=chaves
            )
        else:
            qtd_map = contar_saidas_por_motoboy_dia(
                db, sub_base=sub_base, chaves=chaves
            )

    items = [
        ConferenciaItemOut(
            id=int(r.id),
            sessao_id=int(r.sessao_id) if r.sessao_id is not None else None,
            motoboy_id=int(r.motoboy_id),
            motoboy_nome=nomes.get(int(r.motoboy_id), f"Motoboy {r.motoboy_id}"),
            data_ref=r.data_ref,
            status=r.status,
            qtd_no_momento=(
                qtd_sessao_map.get(int(r.sessao_id), 0)
                if r.sessao_id is not None
                else qtd_map.get((int(r.motoboy_id), r.data_ref), 0)
            ),
            conferido_em=r.conferido_em.isoformat() if r.conferido_em else None,
            ultima_abertura_em=r.ultima_abertura_em.isoformat() if r.ultima_abertura_em else None,
            novos_qtd=(
                novos_map.get((int(r.motoboy_id), r.data_ref), 0)
                if status == STATUS_RECONFERIR
                else None
            ),
        )
        for r in rows
    ]
    items.sort(key=lambda x: (x.motoboy_nome or "").casefold())
    return ConferenciaListOut(items=items, total=len(items))


@router.get("/{motoboy_id}", response_model=ConferenciaDetalheOut)
def detalhe_conferencia(
    motoboy_id: int,
    data_ref: date = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_staff(current_user)
    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(403, "Sub-base não definida.")
    _require_flag(db, current_user, sub_base)

    row = db.scalar(
        select(ConferenciaSaida).where(
            ConferenciaSaida.sub_base == sub_base,
            ConferenciaSaida.motoboy_id == motoboy_id,
            ConferenciaSaida.data_ref == data_ref,
            ConferenciaSaida.sessao_id.is_(None),
        )
    )
    if row is None:
        raise HTTPException(404, "Conferência não encontrada para este motoboy/dia.")

    return _detalhe_out(
        db, sub_base=sub_base, motoboy_id=motoboy_id, data_ref=data_ref, row=row
    )


@router.post("/{motoboy_id}/conferir", response_model=ConferenciaDetalheOut)
def post_conferir(
    motoboy_id: int,
    body: ConferirBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_staff(current_user)
    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(403, "Sub-base não definida.")
    _require_flag(db, current_user, sub_base)

    try:
        row = conferir_saida(
            db,
            sub_base=sub_base,
            motoboy_id=motoboy_id,
            data_ref=body.data_ref,
            user_id=getattr(current_user, "id", None),
        )
        db.commit()
        db.refresh(row)
    except ValueError:
        db.rollback()
        raise HTTPException(404, "Conferência não encontrada para este motoboy/dia.")
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Erro ao conferir saída: {e}")

    return _detalhe_out(
        db, sub_base=sub_base, motoboy_id=motoboy_id, data_ref=body.data_ref, row=row
    )
