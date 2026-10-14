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
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db import SessionLocal, get_db
from auth import get_current_user
from models import (
    BasePreco,
    ColetaExecucao,
    ColetaExecucaoParticipante,
    Entregador,
    EntregadorFechamento,
    EntregadorFechamentoColetaItem,
    EntregadorPreco,
    EntregadorPrecoGlobal,
    Motoboy,
    MotoboySubBase,
    Saida,
    User,
)
from saida_operacional_utils import filtrar_saidas_por_periodo_operacional
from fechamento_pdf_service import build_fechamento_code, get_fechamento_pdf_bytes
from entregador_fechamento_status_pure import (
    STATUS_ELEGIVEIS_PAGAMENTO,
    STATUS_GERADO,
    STATUS_PAGO,
    STATUS_PERMITE_REAJUSTE,
    STATUS_REAJUSTADO,
    normalizar_status_fechamento,
    status_permite_reajuste,
)

from entregador_routes import (
    _resolve_user_base,
    _resolve_executor_scope_ids,
    resolver_precos_entregador,
    resolver_precos_motoboy,
    _calcular_valor_base_motoboy_periodo,
    _calcular_valor_base_periodo,
    _normalizar_servico,
    precos_motoboy_cache,
    valores_base_atuais_motoboys,
    usuario_financeiro,
    STATUS_VALOR_BASE_VALIDOS,
)
from fechamento_valor_pure import ajustes_mudaram, contar_g, saida_cancelada
from pacote_g_categorias import categoria_da_saida, nomes_por_categoria, padrao_id_ou_none

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Fechamentos"])

# Status válidos para saidas no cálculo (fonte única compartilhada)
STATUS_SAIDAS_VALIDOS = STATUS_VALOR_BASE_VALIDOS


def _contar_g_periodo(db: Session, executor_cond, sub_base: str, periodo_inicio: date, periodo_fim: date) -> dict:
    """Pacotes G não cancelados no período, no mesmo recorte do cálculo do valor base."""
    stmt = select(Saida).where(
        Saida.sub_base == sub_base,
        executor_cond,
        Saida.codigo.isnot(None),
        Saida.is_grande.is_(True),
        func.lower(Saida.status).in_(STATUS_SAIDAS_VALIDOS),
        Saida.timestamp >= datetime.combine(periodo_inicio, datetime.min.time()),
        Saida.timestamp < datetime.combine(periodo_fim + timedelta(days=1), datetime.min.time()),
    )
    rows_raw = db.scalars(stmt).all()
    rows, _ = filtrar_saidas_por_periodo_operacional(db, rows_raw, periodo_inicio, periodo_fim)
    out = contar_g(rows)
    nomes = nomes_por_categoria(db, sub_base)
    padrao = padrao_id_ou_none(db, sub_base)
    por_categoria: dict = {}
    for s in rows:
        if saida_cancelada(getattr(s, "status", None)):
            continue
        nome = nomes.get(categoria_da_saida(s, padrao)) or "G"
        por_categoria[nome] = por_categoria.get(nome, 0) + 1
    out["por_categoria"] = por_categoria
    return out


def _contar_g_por_servico_entregador(
    db: Session,
    sub_base: str,
    id_entregador: int,
    periodo_inicio: date,
    periodo_fim: date,
) -> dict:
    entregador_ids, motoboy_ids = _resolve_executor_scope_ids(
        db=db, sub_base_user=sub_base, entregador_id=id_entregador, motoboy_id=None
    )
    conds = []
    if entregador_ids:
        conds.append(Saida.entregador_id.in_(sorted(entregador_ids)))
    if motoboy_ids:
        conds.append(Saida.motoboy_id.in_(sorted(motoboy_ids)))
    if not conds:
        return {"shopee": 0, "ml": 0, "avulso": 0, "total": 0}
    return _contar_g_periodo(db, or_(*conds), sub_base, periodo_inicio, periodo_fim)


def _contar_g_por_servico_motoboy(
    db: Session,
    sub_base: str,
    motoboy_id: int,
    periodo_inicio: date,
    periodo_fim: date,
) -> dict:
    return _contar_g_periodo(db, Saida.motoboy_id == motoboy_id, sub_base, periodo_inicio, periodo_fim)


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
    return normalizar_status_fechamento(fech.status)


def _recalcular_valor_base_fechamento(
    db: Session,
    sub_base: str,
    fech: EntregadorFechamento,
) -> Decimal:
    if getattr(fech, "id_motoboy", None) is not None:
        valor_entregas = _calcular_valor_base_motoboy_periodo(
            db, sub_base, fech.id_motoboy,
            fech.periodo_inicio, fech.periodo_fim,
        )
        _, valor_coletas, _ = _calcular_diarias_coleta_motoboy(
            db, sub_base, fech.id_motoboy, fech.periodo_inicio, fech.periodo_fim
        )
        return (valor_entregas + valor_coletas).quantize(Decimal("0.01"))
    return _calcular_valor_base_periodo(
        db, sub_base, fech.id_entregador,
        fech.periodo_inicio, fech.periodo_fim,
    )


def _valores_base_recalculados(
    db: Session,
    sub_base: str,
    alvos: List[EntregadorFechamento],
) -> dict:
    """Recalcula o valor base do lote numa consulta por período.

    Entregador legado (sem motoboy) continua no cálculo individual.
    """
    out: dict = {}
    motoboys = [f for f in alvos if getattr(f, "id_motoboy", None) is not None]
    grupos: dict = {}
    for fech in motoboys:
        grupos.setdefault((fech.periodo_inicio, fech.periodo_fim), []).append(fech)
    for (inicio, fim), grupo in grupos.items():
        valores = valores_base_atuais_motoboys(
            db,
            sub_base,
            [int(f.id_motoboy) for f in grupo],
            inicio,
            fim,
        )
        for fech in grupo:
            out[int(fech.id_fechamento)] = valores.get(int(fech.id_motoboy), Decimal("0.00"))
    for fech in alvos:
        if getattr(fech, "id_motoboy", None) is None:
            out[int(fech.id_fechamento)] = _recalcular_valor_base_fechamento(db, sub_base, fech)
    return out


def _notificar_pagamentos(sub_base: str, ids_fechamento: List[int]) -> None:
    """Envia o push depois da resposta HTTP, em sessão própria."""
    if not ids_fechamento:
        return
    db = SessionLocal()
    try:
        rows = list(
            db.scalars(
                select(EntregadorFechamento).where(
                    EntregadorFechamento.sub_base == sub_base,
                    EntregadorFechamento.id_fechamento.in_(ids_fechamento),
                )
            ).all()
        )
        for fech in rows:
            _enviar_push_fechamento_pago(db, fech)
    except Exception:
        logger.exception(
            "fechamento_pago_push_lote_failed sub_base=%s qtd=%s",
            sub_base,
            len(ids_fechamento),
        )
    finally:
        db.close()


def _calcular_diarias_coleta_motoboy(
    db: Session,
    sub_base: str,
    motoboy_id: int,
    periodo_inicio: date,
    periodo_fim: date,
) -> tuple[int, Decimal, list[dict]]:
    """Uma diária por motoboy/data, independentemente da quantidade de bases."""
    return calcular_diarias_coleta_motoboys(db, sub_base, [motoboy_id], periodo_inicio, periodo_fim)[int(motoboy_id)]


def calcular_diarias_coleta_motoboys(
    db: Session,
    sub_base: str,
    motoboy_ids: list[int],
    periodo_inicio: date,
    periodo_fim: date,
    *,
    precos_cache: Optional[dict] = None,
) -> dict[int, tuple[int, Decimal, list[dict]]]:
    """Diárias de coleta de vários motoboys do período numa única consulta."""
    ids = sorted({int(m) for m in motoboy_ids if m is not None})
    if not ids:
        return {}
    rows = db.execute(
        select(ColetaExecucaoParticipante.motoboy_id, ColetaExecucao.data_operacao, BasePreco.base)
        .join(
            ColetaExecucaoParticipante,
            ColetaExecucaoParticipante.execucao_id == ColetaExecucao.id_execucao,
        )
        .join(BasePreco, BasePreco.id_base == ColetaExecucao.base_id)
        .where(
            ColetaExecucao.sub_base == sub_base,
            ColetaExecucaoParticipante.sub_base == sub_base,
            ColetaExecucaoParticipante.motoboy_id.in_(ids),
            ColetaExecucaoParticipante.status == "finalizado",
            ColetaExecucao.data_operacao >= periodo_inicio,
            ColetaExecucao.data_operacao <= periodo_fim,
        )
    ).all()
    bases_por_motoboy: dict[int, dict[date, set[str]]] = {mid: {} for mid in ids}
    for motoboy_id, data_operacao, base_nome in rows:
        bases_por_motoboy[int(motoboy_id)].setdefault(data_operacao, set()).add(base_nome)
    out: dict[int, tuple[int, Decimal, list[dict]]] = {}
    for mid, bases_por_dia in bases_por_motoboy.items():
        if not bases_por_dia:
            out[mid] = (0, Decimal("0.00"), [])
            continue
        valor_diaria = Decimal(
            precos_motoboy_cache(db, sub_base, mid, precos_cache).get("coleta_valor") or 0
        ).quantize(Decimal("0.01"))
        itens = [
            {"data": dia, "bases": sorted(bases), "valor_diaria": valor_diaria}
            for dia, bases in sorted(bases_por_dia.items())
        ]
        out[mid] = (len(itens), (valor_diaria * len(itens)).quantize(Decimal("0.01")), itens)
    return out


def _sincronizar_itens_coleta_fechamento(
    db: Session,
    sub_base: str,
    fech: EntregadorFechamento,
    dias_coleta: list[dict],
) -> None:
    """Atualiza o snapshot de diárias deste fechamento, sem pegar dia de outro."""
    motoboy_id = getattr(fech, "id_motoboy", None)
    if motoboy_id is None:
        return
    if dias_coleta:
        dias = [item["data"] for item in dias_coleta]
        dia_ja_pago = db.scalar(
            select(EntregadorFechamentoColetaItem.data).where(
                EntregadorFechamentoColetaItem.sub_base == sub_base,
                EntregadorFechamentoColetaItem.motoboy_id == motoboy_id,
                EntregadorFechamentoColetaItem.data.in_(dias),
                EntregadorFechamentoColetaItem.id_fechamento != fech.id_fechamento,
            )
        )
        if dia_ja_pago:
            raise HTTPException(
                409,
                f"A diária de coleta de {dia_ja_pago.strftime('%d/%m/%Y')} já pertence a outro fechamento.",
            )
    for item in db.scalars(
        select(EntregadorFechamentoColetaItem).where(
            EntregadorFechamentoColetaItem.id_fechamento == fech.id_fechamento
        )
    ).all():
        db.delete(item)
    db.flush()
    for item in dias_coleta:
        db.add(
            EntregadorFechamentoColetaItem(
                id_fechamento=fech.id_fechamento,
                sub_base=sub_base,
                motoboy_id=motoboy_id,
                data=item["data"],
                bases=item["bases"],
                valor_diaria=item["valor_diaria"],
            )
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
    valor_entregas: Decimal = Decimal("0.00")
    valor_coletas: Decimal = Decimal("0.00")
    qtd_dias_coleta: int = 0
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
        valor_entregas=getattr(fech, "valor_entregas", fech.valor_base),
        valor_coletas=getattr(fech, "valor_coletas", Decimal("0.00")),
        qtd_dias_coleta=int(getattr(fech, "qtd_dias_coleta", 0) or 0),
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
    current_user=Depends(usuario_financeiro),
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
        valor_entregas = _calcular_valor_base_motoboy_periodo(
            db, sub_base, motoboy_id, periodo_inicio, periodo_fim
        )
        qtd_dias_coleta, valor_coletas, dias_coleta = _calcular_diarias_coleta_motoboy(
            db, sub_base, motoboy_id, periodo_inicio, periodo_fim
        )
        valor_base = (valor_entregas + valor_coletas).quantize(Decimal("0.01"))
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
            "valor_entregas": valor_entregas,
            "valor_coletas": valor_coletas,
            "qtd_dias_coleta": qtd_dias_coleta,
            "dias_coleta": dias_coleta,
            "entregador_id": None,
            "motoboy_id": motoboy_id,
            "entregador_nome": executor_nome,
            "periodo_inicio": periodo_inicio.isoformat(),
            "periodo_fim": periodo_fim.isoformat(),
            "g_por_servico": {"shopee": g["shopee"], "ml": g["ml"], "avulso": g["avulso"]},
            "g_total": g["total"],
            "g_por_categoria": g.get("por_categoria") or {},
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
        "valor_entregas": valor_base,
        "valor_coletas": Decimal("0.00"),
        "qtd_dias_coleta": 0,
        "dias_coleta": [],
        "entregador_id": entregador_id,
        "motoboy_id": None,
        "entregador_nome": ent.nome or "",
        "periodo_inicio": periodo_inicio.isoformat(),
        "periodo_fim": periodo_fim.isoformat(),
        "g_por_servico": {"shopee": g["shopee"], "ml": g["ml"], "avulso": g["avulso"]},
        "g_total": g["total"],
        "g_por_categoria": g.get("por_categoria") or {},
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
    current_user=Depends(usuario_financeiro),
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
    valores_atuais = valores_base_atuais_motoboys(db, sub_base, motoboy_ids, periodo_inicio, periodo_fim)

    status_filtro = (status or "").strip().upper() or None
    items: List[FechamentoOut] = []
    total_a_pagar = Decimal("0.00")
    total_pago = Decimal("0.00")
    qtd_precisa_reajuste = 0
    qtd_a_pagar = 0
    qtd_pago = 0

    for fech in rows:
        mid = getattr(fech, "id_motoboy", None)
        out = _fechamento_to_out(
            db,
            sub_base,
            fech,
            nomes_motoboy_map=nomes_motoboy_map,
            valor_base_recalc=valores_atuais.get(int(mid)) if mid is not None else None,
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
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
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

    # A tela já confirma o valor gerado. Recalcular o período aqui varre saídas
    # e histórico de novo e segura o registro por minutos, mesmo com 1 motoboy.
    if not payload.confirmar_com_divergencia:
        valores = _valores_base_recalculados(db, sub_base, alvos)
        divergentes_fech = [f for f in alvos if valores.get(int(f.id_fechamento)) != f.valor_base]
        divergentes: List[MarcarPagoDivergenteItem] = []
        if divergentes_fech:
            from motoboy_nome_utils import carregar_nomes_motoboy_ids

            nomes = carregar_nomes_motoboy_ids(
                db,
                [int(f.id_motoboy) for f in divergentes_fech if getattr(f, "id_motoboy", None) is not None],
            )
            for fech in divergentes_fech:
                divergentes.append(
                    MarcarPagoDivergenteItem(
                        id_fechamento=int(fech.id_fechamento),
                        username_entregador=fech.username_entregador,
                        nome_exibicao=_resolver_nome_exibicao_fechamento(db, fech, nomes_motoboy_map=nomes),
                        valor_final=fech.valor_final,
                        valor_base=fech.valor_base,
                        valor_base_recalculado=valores[int(fech.id_fechamento)],
                    )
                )
        if divergentes:
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
    background_tasks.add_task(_notificar_pagamentos, sub_base, ids_marcados)
    logger.info("fechamento_pago_agendado sub_base=%s qtd=%s", sub_base, len(ids_marcados))

    return MarcarPagoResponse(marcados=len(ids_marcados), ids_fechamento=ids_marcados)


# =========================================================
# POST — Criar fechamento
# =========================================================

MSG_FECHAMENTO_JA_EXISTE = "Já existe fechamento para este motoboy neste período."


def _validar_periodo_fechado(periodo_inicio: date, periodo_fim: date) -> None:
    if periodo_inicio > periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")
    if periodo_fim >= date.today():
        raise HTTPException(
            400,
            "Não é permitido criar fechamento para período ainda em aberto. "
            "Escolha um período cuja data final seja anterior à data de hoje.",
        )


def _travar_executor(db: Session, sub_base: str, tipo: str, executor_id: int) -> None:
    """Serializa criações concorrentes do mesmo executor até o fim da transação."""
    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"fechamento:{sub_base}:{tipo}:{int(executor_id)}"},
    )


def _fechamento_sobreposto(
    db: Session,
    sub_base: str,
    *,
    id_entregador: Optional[int],
    id_motoboy: Optional[int],
    periodo_inicio: date,
    periodo_fim: date,
) -> Optional[EntregadorFechamento]:
    if id_motoboy is not None:
        cond = EntregadorFechamento.id_motoboy == id_motoboy
    else:
        cond = EntregadorFechamento.id_entregador == id_entregador
    return db.scalar(
        select(EntregadorFechamento)
        .where(
            EntregadorFechamento.sub_base == sub_base,
            cond,
            EntregadorFechamento.periodo_inicio <= periodo_fim,
            EntregadorFechamento.periodo_fim >= periodo_inicio,
        )
        .order_by(EntregadorFechamento.periodo_inicio)
        .limit(1)
    )


def _erro_sobreposicao(existente: EntregadorFechamento, periodo_inicio: date, periodo_fim: date) -> HTTPException:
    if existente.periodo_inicio == periodo_inicio and existente.periodo_fim == periodo_fim:
        return HTTPException(409, MSG_FECHAMENTO_JA_EXISTE)
    return HTTPException(
        409,
        f"Já existe fechamento de {existente.periodo_inicio.strftime('%d/%m/%Y')} a "
        f"{existente.periodo_fim.strftime('%d/%m/%Y')} que cobre parte deste período.",
    )


def _criar_fechamento_core(
    db: Session,
    sub_base: str,
    payload: FechamentoCreate,
    *,
    pular_sem_valor: bool = False,
) -> Optional[tuple]:
    """Valida, calcula e insere (flush) o fechamento. Não faz commit, PDF nem push.

    Retorna (fechamento, chave_pix) ou None quando pular_sem_valor e não há valor a pagar.
    """
    _validar_periodo_fechado(payload.periodo_inicio, payload.periodo_fim)

    chave_pix: Optional[str] = None
    if payload.id_motoboy is not None:
        motoboy = _resolve_motoboy_subbase(db, sub_base, payload.id_motoboy)
        _travar_executor(db, sub_base, "m", payload.id_motoboy)
        username_ent = _get_motoboy_username(db, motoboy)
        chave_pix = (getattr(motoboy, "chave_pix", None) or "").strip() or None
        id_entregador_val = None
        id_motoboy_val = payload.id_motoboy
        existente = _fechamento_sobreposto(
            db, sub_base, id_entregador=None, id_motoboy=payload.id_motoboy,
            periodo_inicio=payload.periodo_inicio, periodo_fim=payload.periodo_fim,
        )
        valor_entregas = _calcular_valor_base_motoboy_periodo(
            db, sub_base, payload.id_motoboy,
            payload.periodo_inicio, payload.periodo_fim,
        )
        qtd_dias_coleta, valor_coletas, dias_coleta = _calcular_diarias_coleta_motoboy(
            db, sub_base, payload.id_motoboy,
            payload.periodo_inicio, payload.periodo_fim,
        )
        valor_base = (valor_entregas + valor_coletas).quantize(Decimal("0.01"))
    else:
        ent = db.get(Entregador, payload.id_entregador)
        if not ent or ent.sub_base != sub_base:
            raise HTTPException(404, "Entregador não encontrado.")
        _travar_executor(db, sub_base, "e", payload.id_entregador)
        username_ent = ent.username_entregador or ent.nome or ""
        id_entregador_val = payload.id_entregador
        id_motoboy_val = None
        existente = _fechamento_sobreposto(
            db, sub_base, id_entregador=payload.id_entregador, id_motoboy=None,
            periodo_inicio=payload.periodo_inicio, periodo_fim=payload.periodo_fim,
        )
        valor_entregas = _calcular_valor_base_periodo(
            db, sub_base, payload.id_entregador,
            payload.periodo_inicio, payload.periodo_fim,
        )
        valor_coletas = Decimal("0.00")
        qtd_dias_coleta = 0
        dias_coleta = []
        valor_base = valor_entregas

    if existente:
        raise _erro_sobreposicao(existente, payload.periodo_inicio, payload.periodo_fim)

    if id_motoboy_val is not None and dias_coleta:
        dias = [item["data"] for item in dias_coleta]
        dia_ja_pago = db.scalar(
            select(EntregadorFechamentoColetaItem.data).where(
                EntregadorFechamentoColetaItem.sub_base == sub_base,
                EntregadorFechamentoColetaItem.motoboy_id == id_motoboy_val,
                EntregadorFechamentoColetaItem.data.in_(dias),
            )
        )
        if dia_ja_pago:
            raise HTTPException(
                409,
                f"A diária de coleta de {dia_ja_pago.strftime('%d/%m/%Y')} já pertence a outro fechamento.",
            )

    valor_ad = Decimal(str(payload.valor_adicao or 0)).quantize(Decimal("0.01"))
    valor_sub = Decimal(str(payload.valor_subtracao or 0)).quantize(Decimal("0.01"))
    valor_final = (valor_base + valor_ad - valor_sub).quantize(Decimal("0.01"))
    if pular_sem_valor and valor_base == 0 and valor_ad == 0 and valor_sub == 0:
        return None

    fech = EntregadorFechamento(
        sub_base=sub_base,
        id_entregador=id_entregador_val,
        id_motoboy=id_motoboy_val,
        username_entregador=username_ent,
        periodo_inicio=payload.periodo_inicio,
        periodo_fim=payload.periodo_fim,
        valor_base=valor_base,
        valor_entregas=valor_entregas,
        valor_coletas=valor_coletas,
        qtd_dias_coleta=qtd_dias_coleta,
        valor_adicao=valor_ad,
        motivo_adicao=(payload.motivo_adicao or "").strip() or None,
        valor_subtracao=valor_sub,
        motivo_subtracao=(payload.motivo_subtracao or "").strip() or None,
        valor_final=valor_final,
        status=STATUS_GERADO,
    )
    try:
        db.add(fech)
        db.flush()
        if id_motoboy_val is not None:
            for item in dias_coleta:
                db.add(
                    EntregadorFechamentoColetaItem(
                        id_fechamento=fech.id_fechamento,
                        sub_base=sub_base,
                        motoboy_id=id_motoboy_val,
                        data=item["data"],
                        bases=item["bases"],
                        valor_diaria=item["valor_diaria"],
                    )
                )
            db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, MSG_FECHAMENTO_JA_EXISTE)
    return fech, chave_pix


def _pos_criar_fechamento(
    db: Session,
    fech: EntregadorFechamento,
    chave_pix: Optional[str],
) -> EntregadorFechamento:
    """PDF e push após o commit; falhas não desfazem o fechamento."""
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
    return fech


def _commit_criacao(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, MSG_FECHAMENTO_JA_EXISTE)


@router.post("/fechamentos", response_model=FechamentoOut, status_code=201)
def criar_fechamento(
    payload: FechamentoCreate,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    sub_base = _resolve_user_base(db, current_user)
    fech, chave_pix = _criar_fechamento_core(db, sub_base, payload)
    _commit_criacao(db)
    db.refresh(fech)
    fech = _pos_criar_fechamento(db, fech, chave_pix)

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
        valor_entregas=fech.valor_entregas,
        valor_coletas=fech.valor_coletas,
        qtd_dias_coleta=fech.qtd_dias_coleta,
        valor_adicao=fech.valor_adicao,
        motivo_adicao=fech.motivo_adicao,
        valor_subtracao=fech.valor_subtracao,
        motivo_subtracao=fech.motivo_subtracao,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
    )


# =========================================================
# POST — Gerar fechamentos em lote (todos os pendentes do período)
# =========================================================

class FechamentoLoteRequest(BaseModel):
    periodo_inicio: date
    periodo_fim: date
    chave_idempotencia: Optional[str] = Field(None, max_length=80)


class FechamentoLoteItem(BaseModel):
    executor_tipo: str
    executor_id: int
    nome: str
    id_fechamento: Optional[int] = None
    valor_final: Optional[Decimal] = None
    motivo: Optional[str] = None


class FechamentoLoteResponse(BaseModel):
    criados: List[FechamentoLoteItem] = Field(default_factory=list)
    ja_existentes: List[FechamentoLoteItem] = Field(default_factory=list)
    sem_valor: List[FechamentoLoteItem] = Field(default_factory=list)
    erros: List[FechamentoLoteItem] = Field(default_factory=list)
    total_gerado: Decimal = Decimal("0.00")


@router.post("/fechamentos/lote", response_model=FechamentoLoteResponse)
def gerar_fechamentos_lote(
    payload: FechamentoLoteRequest,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    """Gera, sem ajustes, o fechamento de cada executor pendente no período.

    Cada fechamento é confirmado separadamente; repetir a chamada não duplica
    (quem já tem fechamento volta em ja_existentes).
    """
    from entregador_routes import resumo_entregadores

    sub_base = _resolve_user_base(db, current_user)
    _validar_periodo_fechado(payload.periodo_inicio, payload.periodo_fim)
    logger.info(
        "fechamento_lote_inicio sub_base=%s periodo=%s..%s chave=%s",
        sub_base, payload.periodo_inicio, payload.periodo_fim, payload.chave_idempotencia,
    )

    resumo = resumo_entregadores(
        data_inicio=payload.periodo_inicio,
        data_fim=payload.periodo_fim,
        entregador_id=None,
        motoboy_id=None,
        executor_tipo=None,
        executor_id=None,
        fechamento_status=None,
        base=None,
        agrupar="executor",
        page=1,
        pageSize=100000,
        db=db,
        current_user=current_user,
    )
    linhas = list(resumo.items)
    motoboys_na_lista = {r.executor_id for r in linhas if r.executor_tipo == "m"}

    out = FechamentoLoteResponse()
    pos_criacao: list = []
    for r in linhas:
        item = FechamentoLoteItem(executor_tipo=r.executor_tipo, executor_id=r.executor_id, nome=r.nome)
        if r.fechamento_status != "PENDENTE":
            item.id_fechamento = r.id_fechamento
            item.valor_final = r.valor_final
            out.ja_existentes.append(item)
            continue
        if r.parcial:
            item.motivo = "Parte do período já tem fechamento. Gere individualmente."
            out.erros.append(item)
            continue
        if r.executor_tipo == "e":
            _, mids = _resolve_executor_scope_ids(
                db=db, sub_base_user=sub_base, entregador_id=r.executor_id, motoboy_id=None
            )
            if mids & motoboys_na_lista:
                item.motivo = "Registros antigos ligados a um motoboy da lista. Gere individualmente."
                out.erros.append(item)
                continue

        req = FechamentoCreate(
            id_motoboy=r.executor_id if r.executor_tipo == "m" else None,
            id_entregador=r.executor_id if r.executor_tipo == "e" else None,
            periodo_inicio=payload.periodo_inicio,
            periodo_fim=payload.periodo_fim,
        )
        try:
            criado = _criar_fechamento_core(db, sub_base, req, pular_sem_valor=True)
            if criado is None:
                db.rollback()
                out.sem_valor.append(item)
                continue
            fech, chave_pix = criado
            _commit_criacao(db)
        except HTTPException as exc:
            db.rollback()
            if exc.status_code == 409 and exc.detail == MSG_FECHAMENTO_JA_EXISTE:
                out.ja_existentes.append(item)
            else:
                item.motivo = str(exc.detail)
                out.erros.append(item)
            continue
        except Exception:
            logger.exception("fechamento_lote_item_failed sub_base=%s executor=%s_%s", sub_base, r.executor_tipo, r.executor_id)
            db.rollback()
            item.motivo = "Não foi possível gerar. Tente individualmente."
            out.erros.append(item)
            continue

        db.refresh(fech)
        item.id_fechamento = int(fech.id_fechamento)
        item.valor_final = fech.valor_final
        out.criados.append(item)
        out.total_gerado += Decimal(str(fech.valor_final or 0))
        pos_criacao.append((int(fech.id_fechamento), chave_pix))

    for fech_id, chave_pix in pos_criacao:
        fech = db.get(EntregadorFechamento, fech_id)
        if fech is not None:
            _pos_criar_fechamento(db, fech, chave_pix)

    out.total_gerado = out.total_gerado.quantize(Decimal("0.01"))
    logger.info(
        "fechamento_lote_fim sub_base=%s criados=%s existentes=%s sem_valor=%s erros=%s",
        sub_base, len(out.criados), len(out.ja_existentes), len(out.sem_valor), len(out.erros),
    )
    return out


# =========================================================
# GET — Obter fechamento (para modal de edição)
# =========================================================

@router.get("/fechamentos/{id_fechamento}", response_model=FechamentoOut)
def obter_fechamento(
    id_fechamento: int,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
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
    current_user=Depends(usuario_financeiro),
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
    if not status_permite_reajuste(st):
        raise HTTPException(
            400,
            "Apenas fechamentos com status GERADO ou REAJUSTADO podem ser reajustados.",
        )

    chave_pix: Optional[str] = None
    dias_coleta: list[dict] = []
    if getattr(fech, "id_motoboy", None) is not None:
        chave_pix = _get_motoboy_chave_pix(db, fech.id_motoboy)
        valor_entregas = _calcular_valor_base_motoboy_periodo(
            db, sub_base, fech.id_motoboy,
            fech.periodo_inicio, fech.periodo_fim,
        )
        qtd_dias_coleta, valor_coletas, dias_coleta = _calcular_diarias_coleta_motoboy(
            db, sub_base, fech.id_motoboy,
            fech.periodo_inicio, fech.periodo_fim,
        )
        valor_base_recalc = (valor_entregas + valor_coletas).quantize(Decimal("0.01"))
    else:
        valor_entregas = _calcular_valor_base_periodo(
            db, sub_base, fech.id_entregador,
            fech.periodo_inicio, fech.periodo_fim,
        )
        valor_coletas = Decimal("0.00")
        qtd_dias_coleta = 0
        valor_base_recalc = valor_entregas

    novos_valores = {
        "valor_adicao": payload.valor_adicao,
        "motivo_adicao": payload.motivo_adicao,
        "valor_subtracao": payload.valor_subtracao,
        "motivo_subtracao": payload.motivo_subtracao,
    }
    if payload.atualizar_valor_base is True:
        novos_valores["valor_base"] = valor_base_recalc
    valores_atuais = {
        "valor_base": fech.valor_base,
        "valor_adicao": fech.valor_adicao,
        "motivo_adicao": fech.motivo_adicao,
        "valor_subtracao": fech.valor_subtracao,
        "motivo_subtracao": fech.motivo_subtracao,
    }
    if not ajustes_mudaram(valores_atuais, novos_valores):
        return _fechamento_to_out(db, sub_base, fech)

    if payload.atualizar_valor_base is True:
        if getattr(fech, "id_motoboy", None) is not None:
            _sincronizar_itens_coleta_fechamento(db, sub_base, fech, dias_coleta)
        fech.valor_base = valor_base_recalc
        fech.valor_entregas = valor_entregas
        fech.valor_coletas = valor_coletas
        fech.qtd_dias_coleta = qtd_dias_coleta

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
    current_user=Depends(usuario_financeiro),
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
