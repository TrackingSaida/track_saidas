"""
Rotas de Fechamento de Entregador
GET  /entregadores/fechamentos — listar (visão A Pagar)
POST /entregadores/fechamentos/marcar-pago — marcar como pago (lote)
POST /entregadores/fechamentos — criar
PATCH /entregadores/fechamentos/{id_fechamento} — editar/reajustar
GET /entregadores/fechamentos/{id_fechamento} — obter um (para modal)
GET /entregadores/fechamentos/calcular — preview valor base
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import Entregador, EntregadorFechamento, EntregadorPreco, EntregadorPrecoGlobal, Motoboy, MotoboySubBase, Saida, User
from saida_operacional_utils import filtrar_saidas_por_periodo_operacional
from fechamento_pdf_service import build_fechamento_code, get_fechamento_pdf_bytes

from entregador_routes import (
    _resolve_user_base,
    resolver_precos_entregador,
    resolver_precos_motoboy,
    _calcular_valor_base_motoboy_periodo,
    _calcular_valor_base_periodo,
    _normalizar_servico,
    STATUS_VALOR_BASE_VALIDOS,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Fechamentos"])

# Status aceitos
STATUS_GERADO = "GERADO"
STATUS_REAJUSTADO = "REAJUSTADO"
STATUS_PAGO = "PAGO"
STATUS_ELEGIVEIS_PAGAMENTO = (STATUS_GERADO, STATUS_REAJUSTADO)
STATUS_PERMITE_REAJUSTE = (STATUS_GERADO, STATUS_REAJUSTADO)

# Status válidos para saidas no cálculo (fonte única compartilhada)
STATUS_SAIDAS_VALIDOS = STATUS_VALOR_BASE_VALIDOS


def _contar_g_por_servico_entregador(
    db: Session,
    sub_base: str,
    id_entregador: int,
    periodo_inicio: date,
    periodo_fim: date,
) -> dict:
    """Conta saídas com is_grande no período por serviço (shopee, ml, avulso)."""
    stmt = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.entregador_id == id_entregador,
        Saida.is_grande.is_(True),
    )
    stmt = stmt.where(func.lower(Saida.status).in_(STATUS_SAIDAS_VALIDOS))
    rows_raw = db.scalars(stmt).all()
    rows, _ = filtrar_saidas_por_periodo_operacional(db, rows_raw, periodo_inicio, periodo_fim)
    g_shopee = g_ml = g_avulso = 0
    for s in rows:
        t = _normalizar_servico(s.servico)
        if t == "shopee":
            g_shopee += 1
        elif t == "flex":
            g_ml += 1
        else:
            g_avulso += 1
    return {"shopee": g_shopee, "ml": g_ml, "avulso": g_avulso, "total": g_shopee + g_ml + g_avulso}


def _contar_g_por_servico_motoboy(
    db: Session,
    sub_base: str,
    motoboy_id: int,
    periodo_inicio: date,
    periodo_fim: date,
) -> dict:
    """Conta saídas com is_grande no período por serviço (shopee, ml, avulso)."""
    stmt = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.motoboy_id == motoboy_id,
        Saida.is_grande.is_(True),
    )
    stmt = stmt.where(func.lower(Saida.status).in_(STATUS_SAIDAS_VALIDOS))
    rows_raw = db.scalars(stmt).all()
    rows, _ = filtrar_saidas_por_periodo_operacional(db, rows_raw, periodo_inicio, periodo_fim)
    g_shopee = g_ml = g_avulso = 0
    for s in rows:
        t = _normalizar_servico(s.servico)
        if t == "shopee":
            g_shopee += 1
        elif t == "flex":
            g_ml += 1
        else:
            g_avulso += 1
    return {"shopee": g_shopee, "ml": g_ml, "avulso": g_avulso, "total": g_shopee + g_ml + g_avulso}


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
    from motoboy_nome_utils import format_motoboy_nome_parts

    if not motoboy or not motoboy.user_id:
        return f"Motoboy {motoboy.id_motoboy}"
    u = db.get(User, motoboy.user_id)
    if not u:
        return f"Motoboy {motoboy.id_motoboy}"
    if (u.username or "").strip():
        return u.username.strip()
    return format_motoboy_nome_parts(
        u.nome, u.sobrenome, None, motoboy_id=motoboy.id_motoboy
    )


def _get_motoboy_chave_pix(db: Session, motoboy_id: int) -> Optional[str]:
    """Busca a chave PIX atual do motoboy, quando existir."""
    motoboy = db.get(Motoboy, motoboy_id)
    if not motoboy:
        return None
    return (getattr(motoboy, "chave_pix", None) or "").strip() or None


def _resolver_avulso_valor(
    db: Session,
    sub_base: str,
    *,
    id_entregador: Optional[int] = None,
    id_motoboy: Optional[int] = None,
) -> Decimal:
    """Preço unitário de avulso: exceção do motoboy/entregador ou global da sub_base."""
    zero = Decimal("0.00")
    if id_motoboy is not None:
        precos = resolver_precos_motoboy(db, sub_base, id_motoboy)
    elif id_entregador is not None:
        precos = resolver_precos_entregador(db, id_entregador, sub_base)
    else:
        return zero
    try:
        return Decimal(str(precos.get("avulso_valor") or 0)).quantize(Decimal("0.01"))
    except Exception:
        return zero


def _status_norm(fech: EntregadorFechamento) -> str:
    st = (fech.status or "").strip().upper()
    if st == "FECHADO":
        return STATUS_GERADO
    return st


def _recalcular_valor_base_fechamento(
    db: Session,
    sub_base: str,
    fech: EntregadorFechamento,
) -> Decimal:
    if getattr(fech, "id_motoboy", None) is not None:
        return _calcular_valor_base_motoboy_periodo(
            db, sub_base, fech.id_motoboy,
            fech.periodo_inicio, fech.periodo_fim,
        )
    return _calcular_valor_base_periodo(
        db, sub_base, fech.id_entregador,
        fech.periodo_inicio, fech.periodo_fim,
    )


def _fmt_brl_push(v) -> str:
    try:
        n = Decimal(str(v or 0))
    except Exception:
        n = Decimal("0")
    s = f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _enviar_push_fechamento_pago(db: Session, fech: EntregadorFechamento) -> None:
    if not getattr(fech, "id_motoboy", None):
        return
    try:
        from push_notification_service import send_to_motoboy

        periodo = f"{fech.periodo_inicio.strftime('%d/%m')} a {fech.periodo_fim.strftime('%d/%m')}"
        n = send_to_motoboy(
            db,
            motoboy_id=int(fech.id_motoboy),
            sub_base=fech.sub_base,
            tipo="fechamento_pago",
            title="Pagamento realizado",
            body=(
                f"Pagamento de {_fmt_brl_push(fech.valor_final)} "
                f"referente a {periodo} foi confirmado."
            ),
            data={"fechamento_id": fech.id_fechamento},
        )
        db.commit()
        if n <= 0:
            logger.warning(
                "fechamento_pago_push_sem_token id=%s motoboy_id=%s sub_base=%s",
                fech.id_fechamento,
                fech.id_motoboy,
                fech.sub_base,
            )
        else:
            logger.info(
                "fechamento_pago_push_ok id=%s motoboy_id=%s msgs=%s",
                fech.id_fechamento,
                fech.id_motoboy,
                n,
            )
    except Exception:
        logger.exception(
            "fechamento_pago_push_failed id=%s motoboy_id=%s",
            fech.id_fechamento,
            getattr(fech, "id_motoboy", None),
        )
        try:
            db.rollback()
        except Exception:
            pass


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
    nome_exibicao: Optional[str] = None
    chave_pix: Optional[str] = None
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
    pago_em: Optional[datetime] = None
    pago_por: Optional[str] = None
    divergencia_valor_base: Optional[bool] = None  # True = valor_base recalculado diferente do gravado
    valor_base_recalculado: Optional[Decimal] = None  # quando há divergência
    precisa_reajuste: Optional[bool] = None
    alerta_pos_pago: Optional[bool] = None
    tem_pdf: Optional[bool] = None
    codigo: Optional[str] = None
    avulso_valor: Optional[Decimal] = None


class FechamentoListaTotais(BaseModel):
    total_a_pagar: Decimal = Decimal("0.00")
    total_pago: Decimal = Decimal("0.00")
    qtd_precisa_reajuste: int = 0
    qtd_a_pagar: int = 0
    qtd_pago: int = 0


class FechamentoListaResponse(BaseModel):
    items: List[FechamentoOut]
    totais: FechamentoListaTotais


class MarcarPagoRequest(BaseModel):
    periodo_inicio: date
    periodo_fim: date
    todos_elegiveis: bool = False
    ids_fechamento: Optional[List[int]] = None
    confirmar_com_divergencia: bool = False

    @model_validator(mode="after")
    def check_alvo(self):
        if self.periodo_inicio > self.periodo_fim:
            raise ValueError("periodo_inicio deve ser anterior a periodo_fim.")
        if self.todos_elegiveis:
            return self
        if not self.ids_fechamento:
            raise ValueError("Informe ids_fechamento ou todos_elegiveis=true.")
        return self


class MarcarPagoDivergenteItem(BaseModel):
    id_fechamento: int
    username_entregador: Optional[str] = None
    nome_exibicao: Optional[str] = None
    valor_final: Decimal
    valor_base: Decimal
    valor_base_recalculado: Decimal


class MarcarPagoResponse(BaseModel):
    marcados: int
    ids_fechamento: List[int]


def _resolver_nome_exibicao_fechamento(
    db: Session,
    fech: EntregadorFechamento,
    *,
    nomes_motoboy_map: Optional[dict] = None,
) -> str:
    """Nome amigável (nome+sobrenome normalizados) para a tela A Pagar."""
    from motoboy_nome_utils import get_motoboy_display_name
    from name_normalizer import normalize_display_name

    mid = getattr(fech, "id_motoboy", None)
    if mid is not None:
        if nomes_motoboy_map is not None and int(mid) in nomes_motoboy_map:
            return nomes_motoboy_map[int(mid)]
        return get_motoboy_display_name(db, int(mid))

    if fech.id_entregador is not None:
        ent = db.get(Entregador, int(fech.id_entregador))
        if ent and (ent.nome or "").strip():
            return normalize_display_name(ent.nome)

    return normalize_display_name(fech.username_entregador or "") or (
        (fech.username_entregador or "").strip() or "—"
    )


def _fechamento_to_out(
    db: Session,
    sub_base: str,
    fech: EntregadorFechamento,
    *,
    valor_base_recalc: Optional[Decimal] = None,
    incluir_divergencia: bool = True,
    nomes_motoboy_map: Optional[dict] = None,
) -> FechamentoOut:
    st = _status_norm(fech)
    chave_pix: Optional[str] = None
    if getattr(fech, "id_motoboy", None) is not None:
        chave_pix = _get_motoboy_chave_pix(db, fech.id_motoboy)

    divergencia = None
    valor_recalc = None
    precisa_reajuste = None
    alerta_pos_pago = None
    if incluir_divergencia:
        if valor_base_recalc is None:
            valor_base_recalc = _recalcular_valor_base_fechamento(db, sub_base, fech)
        divergencia = valor_base_recalc != fech.valor_base
        if divergencia:
            valor_recalc = valor_base_recalc
            if st in STATUS_PERMITE_REAJUSTE:
                precisa_reajuste = True
            elif st == STATUS_PAGO:
                alerta_pos_pago = True

    nome_exibicao = _resolver_nome_exibicao_fechamento(
        db, fech, nomes_motoboy_map=nomes_motoboy_map
    )

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        id_entregador=fech.id_entregador,
        id_motoboy=getattr(fech, "id_motoboy", None),
        username_entregador=fech.username_entregador,
        nome_exibicao=nome_exibicao,
        chave_pix=chave_pix,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_base=fech.valor_base,
        valor_adicao=fech.valor_adicao,
        motivo_adicao=fech.motivo_adicao,
        valor_subtracao=fech.valor_subtracao,
        motivo_subtracao=fech.motivo_subtracao,
        valor_final=fech.valor_final,
        status=st if st else (fech.status or ""),
        criado_em=fech.criado_em,
        pago_em=getattr(fech, "pago_em", None),
        pago_por=getattr(fech, "pago_por", None),
        divergencia_valor_base=True if divergencia else None,
        valor_base_recalculado=valor_recalc,
        precisa_reajuste=precisa_reajuste,
        alerta_pos_pago=alerta_pos_pago,
        tem_pdf=bool(getattr(fech, "pdf_object_key", None)),
        codigo=build_fechamento_code(fech),
    )

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
    if periodo_fim >= date.today():
        raise HTTPException(
            400,
            "Não é permitido calcular fechamento para período ainda em aberto. "
            "Escolha um período cuja data final seja anterior à data de hoje.",
        )
    if (entregador_id is None) == (motoboy_id is None):
        raise HTTPException(400, "Informe exatamente um de entregador_id ou motoboy_id.")

    from conferencia_saida_service import conferencia_por_dia_periodo, owner_conferencia_habilitada

    conferencia_habilitada = owner_conferencia_habilitada(db, sub_base, current_user)

    if motoboy_id is not None:
        motoboy = _resolve_motoboy_subbase(db, sub_base, motoboy_id)
        valor_base = _calcular_valor_base_motoboy_periodo(
            db, sub_base, motoboy_id, periodo_inicio, periodo_fim
        )
        executor_nome = _get_motoboy_username(db, motoboy)
        g = _contar_g_por_servico_motoboy(db, sub_base, motoboy_id, periodo_inicio, periodo_fim)
        conferencia_por_dia = (
            conferencia_por_dia_periodo(
                db,
                sub_base=sub_base,
                motoboy_id=motoboy_id,
                periodo_inicio=periodo_inicio,
                periodo_fim=periodo_fim,
            )
            if conferencia_habilitada
            else []
        )
        return {
            "valor_base": valor_base,
            "entregador_id": None,
            "motoboy_id": motoboy_id,
            "entregador_nome": executor_nome,
            "periodo_inicio": periodo_inicio.isoformat(),
            "periodo_fim": periodo_fim.isoformat(),
            "g_por_servico": {"shopee": g["shopee"], "ml": g["ml"], "avulso": g["avulso"]},
            "g_total": g["total"],
            "conferencia_habilitada": conferencia_habilitada,
            "conferencia_por_dia": conferencia_por_dia,
            "avulso_valor": _resolver_avulso_valor(db, sub_base, id_motoboy=motoboy_id),
        }

    ent = db.get(Entregador, entregador_id)
    if not ent or ent.sub_base != sub_base:
        raise HTTPException(404, "Entregador não encontrado.")

    valor_base = _calcular_valor_base_periodo(
        db, sub_base, entregador_id, periodo_inicio, periodo_fim
    )
    g = _contar_g_por_servico_entregador(db, sub_base, entregador_id, periodo_inicio, periodo_fim)

    return {
        "valor_base": valor_base,
        "entregador_id": entregador_id,
        "motoboy_id": None,
        "entregador_nome": ent.nome or "",
        "periodo_inicio": periodo_inicio.isoformat(),
        "periodo_fim": periodo_fim.isoformat(),
        "g_por_servico": {"shopee": g["shopee"], "ml": g["ml"], "avulso": g["avulso"]},
        "g_total": g["total"],
        "conferencia_habilitada": conferencia_habilitada,
        "conferencia_por_dia": [],
        "avulso_valor": _resolver_avulso_valor(db, sub_base, id_entregador=entregador_id),
    }


# =========================================================
# GET — Listar fechamentos (visão A Pagar)
# =========================================================

@router.get("/fechamentos", response_model=FechamentoListaResponse)
def listar_fechamentos_admin(
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    status: Optional[str] = Query(None, description="GERADO|REAJUSTADO|PAGO"),
    apenas_com_divergencia: bool = Query(False),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Lista fechamentos consolidados do período (não é o resumo diário)."""
    sub_base = _resolve_user_base(db, current_user)
    if periodo_inicio > periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")

    stmt = select(EntregadorFechamento).where(
        EntregadorFechamento.sub_base == sub_base,
        EntregadorFechamento.periodo_inicio == periodo_inicio,
        EntregadorFechamento.periodo_fim == periodo_fim,
    )
    rows = list(db.scalars(stmt).all())

    from motoboy_nome_utils import carregar_nomes_motoboy_ids
    motoboy_ids = [
        int(f.id_motoboy)
        for f in rows
        if getattr(f, "id_motoboy", None) is not None
    ]
    nomes_motoboy_map = carregar_nomes_motoboy_ids(db, motoboy_ids)

    status_filtro = (status or "").strip().upper() or None
    items: List[FechamentoOut] = []
    total_a_pagar = Decimal("0.00")
    total_pago = Decimal("0.00")
    qtd_precisa_reajuste = 0
    qtd_a_pagar = 0
    qtd_pago = 0

    for fech in rows:
        out = _fechamento_to_out(
            db, sub_base, fech, nomes_motoboy_map=nomes_motoboy_map
        )
        st = (out.status or "").upper()

        if st in STATUS_ELEGIVEIS_PAGAMENTO:
            total_a_pagar += Decimal(str(out.valor_final or 0))
            qtd_a_pagar += 1
        elif st == STATUS_PAGO:
            total_pago += Decimal(str(out.valor_final or 0))
            qtd_pago += 1
        if out.precisa_reajuste:
            qtd_precisa_reajuste += 1

        if status_filtro and st != status_filtro:
            if not (status_filtro == STATUS_GERADO and (fech.status or "").upper() == "FECHADO"):
                continue
        if apenas_com_divergencia and not out.divergencia_valor_base:
            continue
        items.append(out)

    items.sort(
        key=lambda x: (
            (x.nome_exibicao or x.username_entregador or "").casefold(),
            x.id_fechamento,
        )
    )

    return FechamentoListaResponse(
        items=items,
        totais=FechamentoListaTotais(
            total_a_pagar=total_a_pagar.quantize(Decimal("0.01")),
            total_pago=total_pago.quantize(Decimal("0.01")),
            qtd_precisa_reajuste=qtd_precisa_reajuste,
            qtd_a_pagar=qtd_a_pagar,
            qtd_pago=qtd_pago,
        ),
    )


# =========================================================
# POST — Marcar fechamentos como pagos
# =========================================================

@router.post("/fechamentos/marcar-pago", response_model=MarcarPagoResponse)
def marcar_fechamentos_pagos(
    payload: MarcarPagoRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    sub_base = _resolve_user_base(db, current_user)
    pago_por = (getattr(current_user, "username", None) or "").strip() or str(
        getattr(current_user, "id", "") or ""
    )

    stmt = select(EntregadorFechamento).where(
        EntregadorFechamento.sub_base == sub_base,
        EntregadorFechamento.periodo_inicio == payload.periodo_inicio,
        EntregadorFechamento.periodo_fim == payload.periodo_fim,
    )
    rows = list(db.scalars(stmt).all())

    if payload.todos_elegiveis:
        alvos = [f for f in rows if _status_norm(f) in STATUS_ELEGIVEIS_PAGAMENTO]
    else:
        ids = set(int(i) for i in (payload.ids_fechamento or []))
        alvos = []
        by_id = {int(f.id_fechamento): f for f in rows}
        for fid in ids:
            fech = by_id.get(fid)
            if not fech:
                raise HTTPException(
                    404,
                    f"Fechamento {fid} não encontrado no período/sub_base.",
                )
            if _status_norm(fech) not in STATUS_ELEGIVEIS_PAGAMENTO:
                raise HTTPException(
                    400,
                    f"Fechamento {fid} não está elegível para pagamento "
                    f"(status atual: {_status_norm(fech)}).",
                )
            alvos.append(fech)

    if not alvos:
        raise HTTPException(400, "Nenhum fechamento elegível para marcar como pago.")

    divergentes: List[MarcarPagoDivergenteItem] = []
    recalc_cache: dict = {}
    for fech in alvos:
        valor_recalc = _recalcular_valor_base_fechamento(db, sub_base, fech)
        recalc_cache[int(fech.id_fechamento)] = valor_recalc
        if valor_recalc != fech.valor_base:
            divergentes.append(
                MarcarPagoDivergenteItem(
                    id_fechamento=int(fech.id_fechamento),
                    username_entregador=fech.username_entregador,
                    nome_exibicao=_resolver_nome_exibicao_fechamento(db, fech),
                    valor_final=fech.valor_final,
                    valor_base=fech.valor_base,
                    valor_base_recalculado=valor_recalc,
                )
            )

    if divergentes and not payload.confirmar_com_divergencia:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "Há fechamentos com divergência de valor base. "
                    "Confirme explicitamente para marcar como pago mesmo assim."
                ),
                "divergentes": [d.model_dump(mode="json") for d in divergentes],
            },
        )

    agora = datetime.utcnow()
    ids_marcados: List[int] = []
    for fech in alvos:
        fech.status = STATUS_PAGO
        fech.pago_em = agora
        fech.pago_por = pago_por
        ids_marcados.append(int(fech.id_fechamento))

    db.commit()

    for fech in alvos:
        db.refresh(fech)
        _enviar_push_fechamento_pago(db, fech)

    return MarcarPagoResponse(marcados=len(ids_marcados), ids_fechamento=ids_marcados)


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
    if payload.periodo_fim >= date.today():
        raise HTTPException(
            400,
            "Não é permitido criar fechamento para período ainda em aberto. "
            "Escolha um período cuja data final seja anterior à data de hoje.",
        )

    chave_pix: Optional[str] = None
    if payload.id_motoboy is not None:
        motoboy = _resolve_motoboy_subbase(db, sub_base, payload.id_motoboy)
        username_ent = _get_motoboy_username(db, motoboy)
        chave_pix = (getattr(motoboy, "chave_pix", None) or "").strip() or None
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
        valor_base = _calcular_valor_base_periodo(
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

    fech_id = int(fech.id_fechamento)
    try:
        from fechamento_pdf_service import upload_fechamento_pdf

        upload_fechamento_pdf(db, fech, chave_pix=chave_pix)
        db.commit()
    except Exception:
        logger.exception("fechamento_pdf_pos_criar_failed id=%s", fech_id)
        try:
            db.rollback()
        except Exception:
            pass

    fech = db.get(EntregadorFechamento, fech_id) or fech
    if getattr(fech, "id_motoboy", None):
        try:
            from push_notification_service import send_to_motoboy

            periodo = f"{fech.periodo_inicio.strftime('%d/%m')} a {fech.periodo_fim.strftime('%d/%m')}"
            n = send_to_motoboy(
                db,
                motoboy_id=int(fech.id_motoboy),
                sub_base=fech.sub_base,
                tipo="fechamento_pronto",
                title="Fechamento pronto",
                body=f"Seu fechamento de {periodo} está disponível — R$ {fech.valor_final}",
                data={"fechamento_id": fech.id_fechamento},
            )
            db.commit()
            if n <= 0:
                logger.warning(
                    "fechamento_push_sem_token id=%s motoboy_id=%s sub_base=%s",
                    fech.id_fechamento,
                    fech.id_motoboy,
                    fech.sub_base,
                )
            else:
                logger.info(
                    "fechamento_push_ok id=%s motoboy_id=%s msgs=%s",
                    fech.id_fechamento,
                    fech.id_motoboy,
                    n,
                )
        except Exception:
            logger.exception(
                "fechamento_push_failed id=%s motoboy_id=%s",
                fech_id,
                getattr(fech, "id_motoboy", None),
            )
            try:
                db.rollback()
            except Exception:
                pass
        fech = db.get(EntregadorFechamento, fech_id) or fech

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        id_entregador=fech.id_entregador,
        id_motoboy=fech.id_motoboy,
        username_entregador=fech.username_entregador,
        chave_pix=chave_pix,
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

    out = _fechamento_to_out(db, sub_base, fech)
    out.avulso_valor = _resolver_avulso_valor(
        db,
        sub_base,
        id_entregador=out.id_entregador,
        id_motoboy=out.id_motoboy,
    )
    return out


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

    st = _status_norm(fech)
    if st == STATUS_PAGO:
        raise HTTPException(
            400,
            "Fechamentos com status PAGO não podem ser reajustados.",
        )
    if st not in STATUS_PERMITE_REAJUSTE:
        raise HTTPException(
            400,
            "Apenas fechamentos com status GERADO ou REAJUSTADO podem ser reajustados.",
        )

    chave_pix: Optional[str] = None
    if getattr(fech, "id_motoboy", None) is not None:
        chave_pix = _get_motoboy_chave_pix(db, fech.id_motoboy)
        valor_base_recalc = _calcular_valor_base_motoboy_periodo(
            db, sub_base, fech.id_motoboy,
            fech.periodo_inicio, fech.periodo_fim,
        )
    else:
        valor_base_recalc = _calcular_valor_base_periodo(
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

    fech_id = int(fech.id_fechamento)
    try:
        from fechamento_pdf_service import upload_fechamento_pdf

        upload_fechamento_pdf(db, fech, chave_pix=chave_pix)
        db.commit()
    except Exception:
        logger.exception("fechamento_pdf_pos_reajuste_failed id=%s", fech_id)
        try:
            db.rollback()
        except Exception:
            pass

    fech = db.get(EntregadorFechamento, fech_id) or fech
    if getattr(fech, "id_motoboy", None):
        try:
            from push_notification_service import send_to_motoboy

            periodo = f"{fech.periodo_inicio.strftime('%d/%m')} a {fech.periodo_fim.strftime('%d/%m')}"
            n = send_to_motoboy(
                db,
                motoboy_id=int(fech.id_motoboy),
                sub_base=fech.sub_base,
                tipo="fechamento_reajustado",
                title="Fechamento reajustado",
                body=f"Seu fechamento de {periodo} foi atualizado — R$ {fech.valor_final}",
                data={"fechamento_id": fech.id_fechamento},
            )
            db.commit()
            if n <= 0:
                logger.warning(
                    "fechamento_reajuste_push_sem_token id=%s motoboy_id=%s sub_base=%s",
                    fech.id_fechamento,
                    fech.id_motoboy,
                    fech.sub_base,
                )
            else:
                logger.info(
                    "fechamento_reajuste_push_ok id=%s motoboy_id=%s msgs=%s",
                    fech.id_fechamento,
                    fech.id_motoboy,
                    n,
                )
        except Exception:
            logger.exception(
                "fechamento_reajuste_push_failed id=%s motoboy_id=%s",
                fech_id,
                getattr(fech, "id_motoboy", None),
            )
            try:
                db.rollback()
            except Exception:
                pass
        fech = db.get(EntregadorFechamento, fech_id) or fech

    return _fechamento_to_out(db, sub_base, fech, incluir_divergencia=False)

# =========================================================
# GET — PDF oficial do fechamento (mesmo arquivo do mobile)
# =========================================================

@router.get("/fechamentos/{id_fechamento}/pdf")
def baixar_pdf_fechamento_admin(
    id_fechamento: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    sub_base = _resolve_user_base(db, current_user)
    fech = db.get(EntregadorFechamento, id_fechamento)
    if not fech or fech.sub_base != sub_base:
        raise HTTPException(404, "Fechamento não encontrado.")

    chave_pix: Optional[str] = None
    if getattr(fech, "id_motoboy", None) is not None:
        chave_pix = _get_motoboy_chave_pix(db, fech.id_motoboy)

    pdf = get_fechamento_pdf_bytes(db, fech, chave_pix=chave_pix)
    codigo = build_fechamento_code(fech)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{codigo}.pdf"',
        },
    )
