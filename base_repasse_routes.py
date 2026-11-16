"""PRD-011 Fatia 2 — Repasse a pagar para Base parceira (roles 0/1, filtrado por sub_base).

GET   /base-repasse/bases                          — bases que entregam pedidos (+ preço a pagar efetivo)
GET   /base-repasse/bases/{id_base}/preco-pagar    — global, exceção e efetivo
PUT   /base-repasse/bases/{id_base}/preco-pagar    — grava exceção por canal (vazio/igual ao global = sem exceção)
GET   /base-repasse/fechamentos/calcular           — preview do período
GET   /base-repasse/fechamentos                    — lista do período
GET   /base-repasse/fechamentos/{id}               — detalhe com itens por dia
POST  /base-repasse/fechamentos                    — gerar
PATCH /base-repasse/fechamentos/{id}               — reajustar
POST  /base-repasse/fechamentos/marcar-pago        — marcar como pago (lote)
DELETE /base-repasse/fechamentos/{id}              — excluir gerado ou reajustado

Documento A Pagar paralelo ao fechamento de motoboy; não se mistura com A Receber (base_fechamentos).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from base_papel_pure import eh_base_parceira
from base_repasse_pure import (
    CAMPOS_PRECO,
    CANAIS_PRECO,
    STATUS_GERADO,
    STATUS_PAGO,
    STATUS_REAJUSTADO,
    STATUS_REPASSE_CONSULTA,
    calcular_repasse,
    excecoes_preco_pagar,
    normalizar_preco_pagar,
    normalizar_status_repasse,
    origem_precos_pagar,
    periodos_sobrepostos,
    quantizar,
    resolver_precos_pagar,
    status_elegivel_pagamento,
    status_permite_reajuste,
    totais_canais,
    valor_final_repasse,
)
from db import get_db
from entregador_routes import _precos_globais, _resolve_user_base, usuario_financeiro
from models import (
    BasePreco,
    BasePrecoPagar,
    BaseRepasseFechamento,
    BaseRepasseFechamentoItem,
    Saida,
)
from saida_operacional_utils import filtrar_saidas_por_periodo_operacional
from saida_operacional_pure import timestamp_operacional_saida

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/base-repasse", tags=["Repasse a base parceira"])

MSG_FECHAMENTO_JA_EXISTE = "Já existe fechamento de repasse para esta base neste período."


# =========================================================
# Schemas
# =========================================================
class PrecoPagarIn(BaseModel):
    """Vazio ou igual ao global: sem exceção para o canal."""

    shopee_valor: Optional[Decimal] = None
    ml_valor: Optional[Decimal] = None
    avulso_valor: Optional[Decimal] = None


class FechamentoRepasseCreate(BaseModel):
    id_base: int
    periodo_inicio: date
    periodo_fim: date
    valor_adicao: Optional[Decimal] = Field(default=None, ge=0)
    motivo_adicao: Optional[str] = None
    valor_subtracao: Optional[Decimal] = Field(default=None, ge=0)
    motivo_subtracao: Optional[str] = None


class FechamentoRepasseUpdate(BaseModel):
    valor_adicao: Optional[Decimal] = Field(default=None, ge=0)
    motivo_adicao: Optional[str] = None
    valor_subtracao: Optional[Decimal] = Field(default=None, ge=0)
    motivo_subtracao: Optional[str] = None
    # Recontagem das saídas do período com o preço congelado na geração.
    atualizar_valor_base: Optional[bool] = None


class MarcarPagoRepasseRequest(BaseModel):
    periodo_inicio: date
    periodo_fim: date
    ids_fechamento: Optional[List[int]] = None
    todos_elegiveis: bool = False


# =========================================================
# Helpers
# =========================================================
def _dec_json(v: Any) -> Optional[str]:
    return None if v is None else format(quantizar(v), "f")


def _base_do_tenant(db: Session, sub_base: str, id_base: int) -> BasePreco:
    base = db.get(BasePreco, id_base)
    if not base or (base.sub_base or "").strip() != (sub_base or "").strip():
        raise HTTPException(404, "Base não encontrada.")
    return base


def _base_parceira_do_tenant(db: Session, sub_base: str, id_base: int) -> BasePreco:
    base = _base_do_tenant(db, sub_base, id_base)
    if not eh_base_parceira(getattr(base, "papel", None)):
        raise HTTPException(422, "Só uma Base parceira tem preço a pagar.")
    return base


def _precos_view(db: Session, sub_base: str, base: BasePreco) -> Dict[str, Any]:
    zero = Decimal("0.00")
    global_precos = _precos_globais(db, sub_base, zero)
    excecao_row = db.get(BasePrecoPagar, int(base.id_base))
    excecao = (
        {CAMPOS_PRECO[c]: getattr(excecao_row, CAMPOS_PRECO[c]) for c in CANAIS_PRECO}
        if excecao_row is not None and (excecao_row.sub_base or "").strip() == (sub_base or "").strip()
        else None
    )
    efetivo = resolver_precos_pagar(global_precos, excecao)
    origem = origem_precos_pagar(excecao)
    return {
        "id_base": int(base.id_base),
        "base": base.base,
        "entrega_pedidos": bool(base.entrega_pedidos),
        "global": {CAMPOS_PRECO[c]: _dec_json(global_precos.get(CAMPOS_PRECO[c])) for c in CANAIS_PRECO},
        "excecao": {
            CAMPOS_PRECO[c]: _dec_json((excecao or {}).get(CAMPOS_PRECO[c])) for c in CANAIS_PRECO
        },
        "efetivo": {k: _dec_json(v) for k, v in efetivo.items()},
        "origem": origem,
    }


def _validar_periodo_fechado(periodo_inicio: date, periodo_fim: date) -> None:
    if periodo_inicio > periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")
    if periodo_fim >= date.today():
        raise HTTPException(
            400,
            "Não é permitido gerar fechamento para período ainda em aberto. "
            "Escolha um período cuja data final seja anterior à data de hoje.",
        )


def _travar_base(db: Session, sub_base: str, id_base: int) -> None:
    """Serializa criações concorrentes da mesma base até o fim da transação."""
    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"base_repasse:{sub_base}:{int(id_base)}"},
    )


def _fechamento_sobreposto(
    db: Session, sub_base: str, id_base: int, periodo_inicio: date, periodo_fim: date,
    *, ignorar_id: Optional[int] = None,
) -> Optional[BaseRepasseFechamento]:
    stmt = (
        select(BaseRepasseFechamento)
        .where(
            BaseRepasseFechamento.sub_base == sub_base,
            BaseRepasseFechamento.id_base == id_base,
            BaseRepasseFechamento.periodo_inicio <= periodo_fim,
            BaseRepasseFechamento.periodo_fim >= periodo_inicio,
        )
        .order_by(BaseRepasseFechamento.periodo_inicio)
        .limit(1)
    )
    if ignorar_id is not None:
        stmt = stmt.where(BaseRepasseFechamento.id_fechamento != ignorar_id)
    return db.scalar(stmt)


def _erro_sobreposicao(existente: BaseRepasseFechamento, periodo_inicio: date, periodo_fim: date) -> HTTPException:
    if existente.periodo_inicio == periodo_inicio and existente.periodo_fim == periodo_fim:
        return HTTPException(409, MSG_FECHAMENTO_JA_EXISTE)
    return HTTPException(
        409,
        f"Já existe fechamento de {existente.periodo_inicio.strftime('%d/%m/%Y')} a "
        f"{existente.periodo_fim.strftime('%d/%m/%Y')} que cobre parte deste período.",
    )


def _saidas_do_repasse(
    db: Session, sub_base: str, id_base: int, periodo_inicio: date, periodo_fim: date
) -> List[tuple]:
    """(dia operacional, servico, status) das saídas enviadas à base no período."""
    # Sem janela de timestamp: o dia operacional (último evento de atribuição) pode ser
    # posterior à criação da saída. repasse_base_id tem índice parcial e limita o conjunto.
    rows_raw = db.scalars(
        select(Saida).where(
            Saida.sub_base == sub_base,
            Saida.repasse_base_id == id_base,
            Saida.codigo.isnot(None),
            func.lower(Saida.status).in_(STATUS_REPASSE_CONSULTA),
        )
    ).all()
    rows, ctx_map = filtrar_saidas_por_periodo_operacional(db, rows_raw, periodo_inicio, periodo_fim)
    out: List[tuple] = []
    for s in rows:
        ts = timestamp_operacional_saida(ctx_map.get(int(s.id_saida)), s.timestamp)
        if ts is None:
            continue
        out.append((ts.date(), s.servico, s.status))
    return out


def _calcular(
    db: Session, sub_base: str, base: BasePreco, periodo_inicio: date, periodo_fim: date,
    *, precos: Optional[Dict[str, Decimal]] = None,
) -> Dict[str, Any]:
    if precos is None:
        precos = _precos_view_decimais(db, sub_base, base)
    saidas = _saidas_do_repasse(db, sub_base, int(base.id_base), periodo_inicio, periodo_fim)
    itens, valor_base = calcular_repasse(saidas, precos)
    return {"itens": itens, "valor_base": valor_base, "precos": precos, "totais": totais_canais(itens)}


def _precos_view_decimais(db: Session, sub_base: str, base: BasePreco) -> Dict[str, Decimal]:
    global_precos = _precos_globais(db, sub_base, Decimal("0.00"))
    excecao_row = db.get(BasePrecoPagar, int(base.id_base))
    excecao = (
        {CAMPOS_PRECO[c]: getattr(excecao_row, CAMPOS_PRECO[c]) for c in CANAIS_PRECO}
        if excecao_row is not None and (excecao_row.sub_base or "").strip() == (sub_base or "").strip()
        else None
    )
    return resolver_precos_pagar(global_precos, excecao)


def _snapshot_precos(precos: Dict[str, Decimal]) -> Dict[str, str]:
    return {k: format(quantizar(v), "f") for k, v in precos.items()}


def _precos_do_snapshot(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Decimal]:
    snap = snapshot or {}
    return {CAMPOS_PRECO[c]: quantizar(snap.get(CAMPOS_PRECO[c])) for c in CANAIS_PRECO}


def _itens_out(itens) -> List[Dict[str, Any]]:
    out = []
    for i in itens:
        get = (lambda k: i[k]) if isinstance(i, dict) else (lambda k: getattr(i, k))
        out.append(
            {
                "data": get("data").isoformat(),
                "shopee": int(get("shopee") or 0),
                "ml": int(get("ml") or 0),
                "avulso": int(get("avulso") or 0),
                "cancelados": int(get("cancelados") or 0),
                "valor_dia": _dec_json(get("valor_dia")),
            }
        )
    return out


def _fechamento_out(fech: BaseRepasseFechamento, *, com_itens: bool = False) -> Dict[str, Any]:
    itens = sorted(fech.itens or [], key=lambda i: i.data)
    out = {
        "id_fechamento": int(fech.id_fechamento),
        "sub_base": fech.sub_base,
        "id_base": int(fech.id_base),
        "base": fech.base,
        "periodo_inicio": fech.periodo_inicio.isoformat(),
        "periodo_fim": fech.periodo_fim.isoformat(),
        "valor_base": _dec_json(fech.valor_base),
        "valor_adicao": _dec_json(fech.valor_adicao),
        "motivo_adicao": fech.motivo_adicao,
        "valor_subtracao": _dec_json(fech.valor_subtracao),
        "motivo_subtracao": fech.motivo_subtracao,
        "valor_final": _dec_json(fech.valor_final),
        "status": normalizar_status_repasse(fech.status),
        "preco_snapshot": fech.preco_snapshot or {},
        "totais": totais_canais(
            [{"shopee": i.shopee, "ml": i.ml, "avulso": i.avulso, "cancelados": i.cancelados} for i in itens]
        ),
        "criado_em": fech.criado_em.isoformat() if fech.criado_em else None,
        "pago_em": fech.pago_em.isoformat() if fech.pago_em else None,
        "pago_por": fech.pago_por,
    }
    if com_itens:
        out["itens"] = _itens_out(itens)
    return out


def _get_fechamento(db: Session, sub_base: str, id_fechamento: int) -> BaseRepasseFechamento:
    fech = db.get(BaseRepasseFechamento, id_fechamento)
    if not fech or fech.sub_base != sub_base:
        raise HTTPException(404, "Fechamento não encontrado.")
    return fech


def _substituir_itens(db: Session, fech: BaseRepasseFechamento, itens: List[Dict[str, Any]]) -> None:
    fech.itens.clear()
    db.flush()
    for i in itens:
        fech.itens.append(
            BaseRepasseFechamentoItem(
                data=i["data"],
                shopee=i["shopee"],
                ml=i["ml"],
                avulso=i["avulso"],
                cancelados=i["cancelados"],
                valor_dia=i["valor_dia"],
            )
        )


# =========================================================
# Bases que entregam e preço a pagar
# =========================================================
@router.get("/bases")
def listar_bases_que_entregam(
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    sub_base = _resolve_user_base(db, current_user)
    rows = db.scalars(
        select(BasePreco)
        .where(
            BasePreco.sub_base == sub_base,
            BasePreco.papel == "base_parceira",
            BasePreco.entrega_pedidos.is_(True),
            BasePreco.ativo.is_(True),
        )
        .order_by(BasePreco.base)
    ).all()
    return [_precos_view(db, sub_base, b) for b in rows]


@router.get("/bases/{id_base}/preco-pagar")
def obter_preco_pagar(
    id_base: int,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    sub_base = _resolve_user_base(db, current_user)
    base = _base_parceira_do_tenant(db, sub_base, id_base)
    return _precos_view(db, sub_base, base)


@router.put("/bases/{id_base}/preco-pagar")
def salvar_preco_pagar(
    id_base: int,
    payload: PrecoPagarIn,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    sub_base = _resolve_user_base(db, current_user)
    base = _base_parceira_do_tenant(db, sub_base, id_base)
    global_precos = _precos_globais(db, sub_base, Decimal("0.00"))
    try:
        excecoes = excecoes_preco_pagar(payload.model_dump(), global_precos)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    row = db.get(BasePrecoPagar, int(base.id_base))
    if all(v is None for v in excecoes.values()):
        if row is not None and (row.sub_base or "").strip() == sub_base:
            db.delete(row)
    else:
        if row is None:
            row = BasePrecoPagar(id_base=int(base.id_base), sub_base=sub_base)
            db.add(row)
        elif (row.sub_base or "").strip() != sub_base:
            raise HTTPException(404, "Base não encontrada.")
        for campo, valor in excecoes.items():
            setattr(row, campo, valor)
        row.updated_at = datetime.utcnow()
    db.commit()
    return _precos_view(db, sub_base, base)


# =========================================================
# Fechamento A Pagar da base parceira
# =========================================================
@router.get("/fechamentos/calcular")
def calcular_repasse_preview(
    id_base: int = Query(...),
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    sub_base = _resolve_user_base(db, current_user)
    _validar_periodo_fechado(periodo_inicio, periodo_fim)
    base = _base_parceira_do_tenant(db, sub_base, id_base)
    calc = _calcular(db, sub_base, base, periodo_inicio, periodo_fim)
    existente = _fechamento_sobreposto(db, sub_base, int(base.id_base), periodo_inicio, periodo_fim)
    return {
        "id_base": int(base.id_base),
        "base": base.base,
        "periodo_inicio": periodo_inicio.isoformat(),
        "periodo_fim": periodo_fim.isoformat(),
        "valor_base": _dec_json(calc["valor_base"]),
        "precos": _snapshot_precos(calc["precos"]),
        "totais": calc["totais"],
        "itens": _itens_out(calc["itens"]),
        "fechamento_existente_id": int(existente.id_fechamento) if existente else None,
    }


@router.get("/fechamentos")
def listar_fechamentos_repasse(
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    status: Optional[str] = Query(None, description="GERADO|REAJUSTADO|PAGO"),
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    sub_base = _resolve_user_base(db, current_user)
    if periodo_inicio > periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")
    rows = db.scalars(
        select(BaseRepasseFechamento).where(
            BaseRepasseFechamento.sub_base == sub_base,
            BaseRepasseFechamento.periodo_inicio == periodo_inicio,
            BaseRepasseFechamento.periodo_fim == periodo_fim,
        )
    ).all()
    status_filtro = normalizar_status_repasse(status) or None
    items = []
    total_a_pagar = total_pago = Decimal("0.00")
    for f in rows:
        st = normalizar_status_repasse(f.status)
        if st in (STATUS_GERADO, STATUS_REAJUSTADO):
            total_a_pagar += quantizar(f.valor_final)
        elif st == STATUS_PAGO:
            total_pago += quantizar(f.valor_final)
        if status_filtro and st != status_filtro:
            continue
        items.append(_fechamento_out(f))
    items.sort(key=lambda x: ((x["base"] or "").casefold(), x["id_fechamento"]))
    return {
        "items": items,
        "totais": {
            "total_a_pagar": _dec_json(total_a_pagar),
            "total_pago": _dec_json(total_pago),
        },
    }


@router.post("/fechamentos/marcar-pago")
def marcar_repasses_pagos(
    payload: MarcarPagoRepasseRequest,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    sub_base = _resolve_user_base(db, current_user)
    pago_por = (getattr(current_user, "username", None) or "").strip() or str(
        getattr(current_user, "id", "") or ""
    )
    rows = list(
        db.scalars(
            select(BaseRepasseFechamento).where(
                BaseRepasseFechamento.sub_base == sub_base,
                BaseRepasseFechamento.periodo_inicio == payload.periodo_inicio,
                BaseRepasseFechamento.periodo_fim == payload.periodo_fim,
            )
        ).all()
    )
    if payload.todos_elegiveis:
        alvos = [f for f in rows if status_elegivel_pagamento(f.status)]
    else:
        by_id = {int(f.id_fechamento): f for f in rows}
        alvos = []
        for fid in {int(i) for i in (payload.ids_fechamento or [])}:
            fech = by_id.get(fid)
            if not fech:
                raise HTTPException(404, f"Fechamento {fid} não encontrado no período/sub_base.")
            if not status_elegivel_pagamento(fech.status):
                raise HTTPException(
                    400,
                    f"Fechamento {fid} não está elegível para pagamento "
                    f"(status atual: {normalizar_status_repasse(fech.status)}).",
                )
            alvos.append(fech)
    if not alvos:
        raise HTTPException(400, "Nenhum fechamento elegível para marcar como pago.")
    agora = datetime.utcnow()
    for f in alvos:
        f.status = STATUS_PAGO
        f.pago_em = agora
        f.pago_por = pago_por
    db.commit()
    return {"marcados": len(alvos), "ids_fechamento": [int(f.id_fechamento) for f in alvos]}


@router.post("/fechamentos", status_code=201)
def gerar_fechamento_repasse(
    payload: FechamentoRepasseCreate,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    sub_base = _resolve_user_base(db, current_user)
    _validar_periodo_fechado(payload.periodo_inicio, payload.periodo_fim)
    base = _base_parceira_do_tenant(db, sub_base, payload.id_base)
    _travar_base(db, sub_base, int(base.id_base))

    existente = _fechamento_sobreposto(
        db, sub_base, int(base.id_base), payload.periodo_inicio, payload.periodo_fim
    )
    if existente:
        raise _erro_sobreposicao(existente, payload.periodo_inicio, payload.periodo_fim)

    calc = _calcular(db, sub_base, base, payload.periodo_inicio, payload.periodo_fim)
    valor_ad = quantizar(payload.valor_adicao)
    valor_sub = quantizar(payload.valor_subtracao)
    fech = BaseRepasseFechamento(
        sub_base=sub_base,
        id_base=int(base.id_base),
        base=(base.base or "").strip(),
        periodo_inicio=payload.periodo_inicio,
        periodo_fim=payload.periodo_fim,
        valor_base=calc["valor_base"],
        valor_adicao=valor_ad,
        motivo_adicao=(payload.motivo_adicao or "").strip() or None,
        valor_subtracao=valor_sub,
        motivo_subtracao=(payload.motivo_subtracao or "").strip() or None,
        valor_final=valor_final_repasse(calc["valor_base"], valor_ad, valor_sub),
        status=STATUS_GERADO,
        preco_snapshot=_snapshot_precos(calc["precos"]),
    )
    try:
        db.add(fech)
        db.flush()
        _substituir_itens(db, fech, calc["itens"])
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, MSG_FECHAMENTO_JA_EXISTE)
    db.refresh(fech)
    return _fechamento_out(fech, com_itens=True)


@router.get("/fechamentos/{id_fechamento}")
def obter_fechamento_repasse(
    id_fechamento: int,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    sub_base = _resolve_user_base(db, current_user)
    return _fechamento_out(_get_fechamento(db, sub_base, id_fechamento), com_itens=True)


@router.patch("/fechamentos/{id_fechamento}")
def reajustar_fechamento_repasse(
    id_fechamento: int,
    payload: FechamentoRepasseUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    sub_base = _resolve_user_base(db, current_user)
    fech = _get_fechamento(db, sub_base, id_fechamento)
    st = normalizar_status_repasse(fech.status)
    if st == STATUS_PAGO:
        raise HTTPException(400, "Fechamentos com status PAGO não podem ser reajustados.")
    if not status_permite_reajuste(st):
        raise HTTPException(400, "Apenas fechamentos com status GERADO ou REAJUSTADO podem ser reajustados.")

    mudou = False
    if payload.atualizar_valor_base is True:
        base = _base_do_tenant(db, sub_base, int(fech.id_base))
        # Preço congelado na geração; só as saídas do período são recontadas.
        calc = _calcular(
            db, sub_base, base, fech.periodo_inicio, fech.periodo_fim,
            precos=_precos_do_snapshot(fech.preco_snapshot),
        )
        _substituir_itens(db, fech, calc["itens"])
        if quantizar(fech.valor_base) != calc["valor_base"]:
            mudou = True
        fech.valor_base = calc["valor_base"]
    if payload.valor_adicao is not None:
        novo = quantizar(payload.valor_adicao)
        mudou = mudou or novo != quantizar(fech.valor_adicao)
        fech.valor_adicao = novo
    if payload.motivo_adicao is not None:
        novo_motivo = (payload.motivo_adicao or "").strip() or None
        mudou = mudou or novo_motivo != (fech.motivo_adicao or None)
        fech.motivo_adicao = novo_motivo
    if payload.valor_subtracao is not None:
        novo = quantizar(payload.valor_subtracao)
        mudou = mudou or novo != quantizar(fech.valor_subtracao)
        fech.valor_subtracao = novo
    if payload.motivo_subtracao is not None:
        novo_motivo = (payload.motivo_subtracao or "").strip() or None
        mudou = mudou or novo_motivo != (fech.motivo_subtracao or None)
        fech.motivo_subtracao = novo_motivo
    if not mudou:
        return _fechamento_out(fech, com_itens=True)

    fech.valor_final = valor_final_repasse(fech.valor_base, fech.valor_adicao, fech.valor_subtracao)
    fech.status = STATUS_REAJUSTADO
    db.commit()
    db.refresh(fech)
    return _fechamento_out(fech, com_itens=True)


@router.delete("/fechamentos/{id_fechamento}", status_code=204)
def excluir_fechamento_repasse(
    id_fechamento: int,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    sub_base = _resolve_user_base(db, current_user)
    fech = _get_fechamento(db, sub_base, id_fechamento)
    if not status_permite_reajuste(fech.status):
        raise HTTPException(400, "Só é possível excluir fechamentos GERADO ou REAJUSTADO.")
    db.delete(fech)
    db.commit()
    return None
