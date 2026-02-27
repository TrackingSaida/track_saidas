"""
Rotas de Fechamento de Entregador
POST /entregadores/fechamentos — criar
PATCH /entregadores/fechamentos/{id_fechamento} — editar/reabrir
GET /entregadores/fechamentos/{id_fechamento} — obter um (para modal)
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import Entregador, EntregadorFechamento, EntregadorPreco, EntregadorPrecoGlobal, Motoboy, MotoboySubBase, Saida, User

from entregador_routes import (
    _resolve_user_base,
    resolver_precos_entregador,
    resolver_precos_motoboy,
    _calcular_valor_base_motoboy_periodo,
    _normalizar_servico,
)

router = APIRouter(prefix="", tags=["Fechamentos"])

# Status aceitos
STATUS_GERADO = "GERADO"
STATUS_REAJUSTADO = "REAJUSTADO"

# Status válidos para saidas no cálculo (alinhado ao app mobile)
STATUS_SAIDAS_VALIDOS = ["saiu", "saiu pra entrega", "saiu_pra_entrega", "em_rota", "entregue", "ausente"]


def _resolve_motoboy_subbase(db: Session, sub_base: str, motoboy_id: int) -> Motoboy:
    """Retorna o Motoboy se existir e estiver vinculado à sub_base."""
    motoboy = db.get(Motoboy, motoboy_id)
    if not motoboy:
        raise HTTPException(404, "Motoboy não encontrado.")
    vinc = db.scalar(
        select(MotoboySubBase).where(
            MotoboySubBase.motoboy_id == motoboy_id,
            MotoboySubBase.sub_base == sub_base,
            MotoboySubBase.ativo.is_(True),
        )
    )
    if not vinc:
        raise HTTPException(422, "Motoboy não vinculado a esta sub_base.")
    return motoboy


def _get_motoboy_username(db: Session, motoboy: Motoboy) -> str:
    """Username ou nome do User do motoboy para username_entregador."""
    if not motoboy or not motoboy.user_id:
        return f"Motoboy {motoboy.id_motoboy}"
    u = db.get(User, motoboy.user_id)
    if not u:
        return f"Motoboy {motoboy.id_motoboy}"
    return (u.username or f"{u.nome or ''} {u.sobrenome or ''}".strip() or f"Motoboy {motoboy.id_motoboy}").strip()


def _calcular_valor_base(
    db: Session,
    sub_base: str,
    id_entregador: int,
    periodo_inicio: date,
    periodo_fim: date,
) -> Decimal:
    """
    Calcula o valor_base a partir das saidas do entregador no período.
    Usa a mesma lógica do resumo por entregador.
    """
    stmt = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.entregador_id == id_entregador,
        Saida.codigo.isnot(None),
        Saida.timestamp >= datetime.combine(periodo_inicio, time.min),
        Saida.timestamp <= datetime.combine(periodo_fim, time(23, 59, 59)),
    )
    from sqlalchemy import func
    stmt = stmt.where(func.lower(Saida.status).in_(STATUS_SAIDAS_VALIDOS))
    rows = db.scalars(stmt).all()

    precos = resolver_precos_entregador(db, id_entregador, sub_base)
    total = Decimal("0.00")

    for saida in rows:
        tipo = _normalizar_servico(saida.servico)
        if tipo == "shopee":
            total += precos["shopee_valor"]
        elif tipo == "flex":
            total += precos["ml_valor"]
        else:
            total += precos["avulso_valor"]

    return total.quantize(Decimal("0.01"))


def _buscar_fechamento_por_data(
    db: Session,
    sub_base: str,
    id_entregador: int,
    data_ref: date,
) -> Optional[EntregadorFechamento]:
    """Retorna o fechamento que cobre a data_ref para o entregador, se existir."""
    return db.scalars(
        select(EntregadorFechamento).where(
            EntregadorFechamento.sub_base == sub_base,
            EntregadorFechamento.id_entregador == id_entregador,
            EntregadorFechamento.periodo_inicio <= data_ref,
            EntregadorFechamento.periodo_fim >= data_ref,
        )
    ).first()


# =========================================================
# SCHEMAS
# =========================================================

class FechamentoCreate(BaseModel):
    id_entregador: Optional[int] = Field(None, gt=0)
    id_motoboy: Optional[int] = Field(None, gt=0)
    periodo_inicio: date
    periodo_fim: date
    valor_adicao: Optional[Decimal] = Decimal("0.00")
    motivo_adicao: Optional[str] = None
    valor_subtracao: Optional[Decimal] = Decimal("0.00")
    motivo_subtracao: Optional[str] = None

    @model_validator(mode="after")
    def check_actor(self):
        if (self.id_entregador is None) == (self.id_motoboy is None):
            raise ValueError("Informe exatamente um de id_entregador ou id_motoboy.")
        return self


class FechamentoUpdate(BaseModel):
    valor_adicao: Optional[Decimal] = None
    motivo_adicao: Optional[str] = None
    valor_subtracao: Optional[Decimal] = None
    motivo_subtracao: Optional[str] = None
    atualizar_valor_base: Optional[bool] = None  # True = usar valor_base recalculado


class FechamentoOut(BaseModel):
    id_fechamento: int
    sub_base: str
    id_entregador: Optional[int] = None
    id_motoboy: Optional[int] = None
    username_entregador: Optional[str] = None
    periodo_inicio: date
    periodo_fim: date
    valor_base: Decimal
    valor_adicao: Decimal
    motivo_adicao: Optional[str] = None
    valor_subtracao: Decimal
    motivo_subtracao: Optional[str] = None
    valor_final: Decimal
    status: str
    criado_em: Optional[datetime] = None
    divergencia_valor_base: Optional[bool] = None  # True = valor_base recalculado diferente do gravado
    valor_base_recalculado: Optional[Decimal] = None  # quando há divergência


# =========================================================
# GET — Calcular valor_base (preview para modal)
# =========================================================

@router.get("/fechamentos/calcular")
def calcular_valor_base_preview(
    entregador_id: Optional[int] = Query(None),
    motoboy_id: Optional[int] = Query(None),
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Retorna valor_base calculado para o período (sem criar fechamento). Informe entregador_id ou motoboy_id."""
    sub_base = _resolve_user_base(db, current_user)

    if periodo_inicio > periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")
    if (entregador_id is None) == (motoboy_id is None):
        raise HTTPException(400, "Informe exatamente um de entregador_id ou motoboy_id.")

    if motoboy_id is not None:
        motoboy = _resolve_motoboy_subbase(db, sub_base, motoboy_id)
        valor_base = _calcular_valor_base_motoboy_periodo(
            db, sub_base, motoboy_id, periodo_inicio, periodo_fim
        )
        executor_nome = _get_motoboy_username(db, motoboy)
        return {
            "valor_base": valor_base,
            "entregador_id": None,
            "motoboy_id": motoboy_id,
            "entregador_nome": executor_nome,
            "periodo_inicio": periodo_inicio.isoformat(),
            "periodo_fim": periodo_fim.isoformat(),
        }

    ent = db.get(Entregador, entregador_id)
    if not ent or ent.sub_base != sub_base:
        raise HTTPException(404, "Entregador não encontrado.")

    valor_base = _calcular_valor_base(
        db, sub_base, entregador_id, periodo_inicio, periodo_fim
    )

    return {
        "valor_base": valor_base,
        "entregador_id": entregador_id,
        "motoboy_id": None,
        "entregador_nome": ent.nome or "",
        "periodo_inicio": periodo_inicio.isoformat(),
        "periodo_fim": periodo_fim.isoformat(),
    }


# =========================================================
# POST — Criar fechamento
# =========================================================

@router.post("/fechamentos", response_model=FechamentoOut, status_code=201)
def criar_fechamento(
    payload: FechamentoCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    sub_base = _resolve_user_base(db, current_user)

    if payload.periodo_inicio > payload.periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")

    if payload.id_motoboy is not None:
        motoboy = _resolve_motoboy_subbase(db, sub_base, payload.id_motoboy)
        username_ent = _get_motoboy_username(db, motoboy)
        id_entregador_val = None
        id_motoboy_val = payload.id_motoboy
        existente = db.scalar(
            select(EntregadorFechamento).where(
                EntregadorFechamento.sub_base == sub_base,
                EntregadorFechamento.id_motoboy == payload.id_motoboy,
                EntregadorFechamento.periodo_inicio == payload.periodo_inicio,
                EntregadorFechamento.periodo_fim == payload.periodo_fim,
            )
        )
        valor_base = _calcular_valor_base_motoboy_periodo(
            db, sub_base, payload.id_motoboy,
            payload.periodo_inicio, payload.periodo_fim,
        )
    else:
        ent = db.get(Entregador, payload.id_entregador)
        if not ent or ent.sub_base != sub_base:
            raise HTTPException(404, "Entregador não encontrado.")
        username_ent = ent.username_entregador or ent.nome or ""
        id_entregador_val = payload.id_entregador
        id_motoboy_val = None
        existente = db.scalar(
            select(EntregadorFechamento).where(
                EntregadorFechamento.sub_base == sub_base,
                EntregadorFechamento.id_entregador == payload.id_entregador,
                EntregadorFechamento.periodo_inicio == payload.periodo_inicio,
                EntregadorFechamento.periodo_fim == payload.periodo_fim,
            )
        )
        valor_base = _calcular_valor_base(
            db, sub_base, payload.id_entregador,
            payload.periodo_inicio, payload.periodo_fim,
        )

    if existente:
        raise HTTPException(
            409,
            "Já existe fechamento para este executor e período."
        )

    valor_ad = Decimal(str(payload.valor_adicao or 0)).quantize(Decimal("0.01"))
    valor_sub = Decimal(str(payload.valor_subtracao or 0)).quantize(Decimal("0.01"))
    valor_final = (valor_base + valor_ad - valor_sub).quantize(Decimal("0.01"))

    fech = EntregadorFechamento(
        sub_base=sub_base,
        id_entregador=id_entregador_val,
        id_motoboy=id_motoboy_val,
        username_entregador=username_ent,
        periodo_inicio=payload.periodo_inicio,
        periodo_fim=payload.periodo_fim,
        valor_base=valor_base,
        valor_adicao=valor_ad,
        motivo_adicao=(payload.motivo_adicao or "").strip() or None,
        valor_subtracao=valor_sub,
        motivo_subtracao=(payload.motivo_subtracao or "").strip() or None,
        valor_final=valor_final,
        status=STATUS_GERADO,
    )
    db.add(fech)
    db.commit()
    db.refresh(fech)

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        id_entregador=fech.id_entregador,
        id_motoboy=fech.id_motoboy,
        username_entregador=fech.username_entregador,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_base=fech.valor_base,
        valor_adicao=fech.valor_adicao,
        motivo_adicao=fech.motivo_adicao,
        valor_subtracao=fech.valor_subtracao,
        motivo_subtracao=fech.motivo_subtracao,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
    )


# =========================================================
# GET — Obter fechamento (para modal de edição)
# =========================================================

@router.get("/fechamentos/{id_fechamento}", response_model=FechamentoOut)
def obter_fechamento(
    id_fechamento: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    sub_base = _resolve_user_base(db, current_user)

    fech = db.get(EntregadorFechamento, id_fechamento)
    if not fech or fech.sub_base != sub_base:
        raise HTTPException(404, "Fechamento não encontrado.")

    if getattr(fech, "id_motoboy", None) is not None:
        valor_base_recalc = _calcular_valor_base_motoboy_periodo(
            db, sub_base, fech.id_motoboy,
            fech.periodo_inicio, fech.periodo_fim,
        )
    else:
        valor_base_recalc = _calcular_valor_base(
            db, sub_base, fech.id_entregador,
            fech.periodo_inicio, fech.periodo_fim,
        )
    divergencia = valor_base_recalc != fech.valor_base

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        id_entregador=fech.id_entregador,
        id_motoboy=getattr(fech, "id_motoboy", None),
        username_entregador=fech.username_entregador,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_base=fech.valor_base,
        valor_adicao=fech.valor_adicao,
        motivo_adicao=fech.motivo_adicao,
        valor_subtracao=fech.valor_subtracao,
        motivo_subtracao=fech.motivo_subtracao,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        divergencia_valor_base=divergencia if divergencia else None,
        valor_base_recalculado=valor_base_recalc if divergencia else None,
    )


# =========================================================
# PATCH — Editar / Reabrir fechamento
# =========================================================

@router.patch("/fechamentos/{id_fechamento}", response_model=FechamentoOut)
def atualizar_fechamento(
    id_fechamento: int,
    payload: FechamentoUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    sub_base = _resolve_user_base(db, current_user)

    fech = db.get(EntregadorFechamento, id_fechamento)
    if not fech or fech.sub_base != sub_base:
        raise HTTPException(404, "Fechamento não encontrado.")
    if (fech.status or "").upper() != STATUS_GERADO:
        raise HTTPException(
            400,
            "Apenas fechamentos com status GERADO podem ser reajustados.",
        )

    if getattr(fech, "id_motoboy", None) is not None:
        valor_base_recalc = _calcular_valor_base_motoboy_periodo(
            db, sub_base, fech.id_motoboy,
            fech.periodo_inicio, fech.periodo_fim,
        )
    else:
        valor_base_recalc = _calcular_valor_base(
            db, sub_base, fech.id_entregador,
            fech.periodo_inicio, fech.periodo_fim,
        )

    if payload.atualizar_valor_base is True:
        fech.valor_base = valor_base_recalc

    # Atualizar adição/subtração
    if payload.valor_adicao is not None:
        fech.valor_adicao = Decimal(str(payload.valor_adicao)).quantize(Decimal("0.01"))
    if payload.motivo_adicao is not None:
        fech.motivo_adicao = (payload.motivo_adicao or "").strip() or None
    if payload.valor_subtracao is not None:
        fech.valor_subtracao = Decimal(str(payload.valor_subtracao)).quantize(Decimal("0.01"))
    if payload.motivo_subtracao is not None:
        fech.motivo_subtracao = (payload.motivo_subtracao or "").strip() or None

    # Recalcular valor_final
    fech.valor_final = (
        fech.valor_base + fech.valor_adicao - fech.valor_subtracao
    ).quantize(Decimal("0.01"))

    fech.status = STATUS_REAJUSTADO

    db.commit()
    db.refresh(fech)

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        id_entregador=fech.id_entregador,
        id_motoboy=getattr(fech, "id_motoboy", None),
        username_entregador=fech.username_entregador,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_base=fech.valor_base,
        valor_adicao=fech.valor_adicao,
        motivo_adicao=fech.motivo_adicao,
        valor_subtracao=fech.valor_subtracao,
        motivo_subtracao=fech.motivo_subtracao,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
    )
